#!/usr/bin/python
# coding: utf-8

import numpy as np
from numpy import linalg
from sklearn.mixture import GMM
import scipy.linalg
import scipy.sparse
import scipy.sparse.linalg


class GMMMap:

    """GMM-based frame-by-frame speech parameter mapping.

    GMMMap represents a class to transform spectral features of a source
    speaker to that of a target speaker based on Gaussian Mixture Models
    of source and target joint spectral features.

    Notation
    --------
    Source speaker's feature: X = {x_t}, 0 <= t < T
    Target speaker's feature: Y = {y_t}, 0 <= t < T
    where T is the number of time frames.

    Parameters
    ----------
    gmm : sklearn.mixture.GMM
        Gaussian Mixture Models of source and target joint features

    swap : bool
        True: source -> target
        False target -> source

    Attributes
    ----------
    num_mixtures : int
        the number of Gaussian mixtures

    weights : array, shape (`num_mixtures`)
        weights of each Gaussian

    src_means : array, shape (`num_mixtures`, `order of spectral feature`)
        means of GMM for a source speaker

    tgt_means : array, shape (`num_mixtures`, `order of spectral feature`)
        means of GMM for a target speaker

    covarXX : array, shape (`num_mixtures`, `order of spectral feature`,
        `order of spectral feature`)
        variance matrix of source speaker's spectral feature

    covarXY : array, shape (`num_mixtures`, `order of spectral feature`,
        `order of spectral feature`)
        covariance matrix of source and target speaker's spectral feature

    covarYX : array, shape (`num_mixtures`, `order of spectral feature`,
        `order of spectral feature`)
        covariance matrix of target and source speaker's spectral feature

    covarYY : array, shape (`num_mixtures`, `order of spectral feature`,
        `order of spectral feature`)
        variance matrix of target speaker's spectral feature

    D : array, shape (`num_mixtures`, `order of spectral feature`,
        `order of spectral feature`)
        covariance matrices of target static spectral features

    px : sklearn.mixture.GMM
        Gaussian Mixture Models of source speaker's features

    Reference
    ---------
      - [Toda 2007] Voice Conversion Based on Maximum Likelihood Estimation
        of Spectral Parameter Trajectory.
        http://isw3.naist.jp/~tomoki/Tomoki/Journals/IEEE-Nov-2007_MLVC.pdf

    """

    def __init__(self, gmm, swap=False):
        # D is the order of spectral feature for a speaker
        self.num_mixtures, D = gmm.means_.shape[0], gmm.means_.shape[1] / 2
        self.weights = gmm.weights_

        # Split source and target parameters from joint GMM
        self.src_means = gmm.means_[:, 0:D]
        self.tgt_means = gmm.means_[:, D:]
        self.covarXX = gmm.covars_[:, :D, :D]
        self.covarXY = gmm.covars_[:, :D, D:]
        self.covarYX = gmm.covars_[:, D:, :D]
        self.covarYY = gmm.covars_[:, D:, D:]

        # swap src and target parameters
        if swap:
            self.tgt_means, self.src_means = self.src_means, self.tgt_means
            self.covarYY, self.covarXX = self.covarXX, self.covarYY
            self.covarYX, self.covarXY = self.XY, self.covarYX

        # Compute D eq.(12) in [Toda 2007]
        self.D = np.zeros(
            self.num_mixtures * D * D).reshape(self.num_mixtures, D, D)
        for m in range(self.num_mixtures):
            xx_inv_xy = np.linalg.solve(self.covarXX[m], self.covarXY[m])
            self.D[m] = self.covarYY[m] - np.dot(self.covarYX[m], xx_inv_xy)

        # p(x), which is used to compute posterior prob. for a given source
        # spectral feature in mapping stage.
        self.px = GMM(n_components=self.num_mixtures, covariance_type="full")
        self.px.means_ = self.src_means
        self.px.covars_ = self.covarXX
        self.px.weights_ = self.weights

    def convert(self, src):
        """
        Mapping source spectral feature x to target spectral feature y
        so that minimize the mean least squared error.
        More specifically, it returns the value E(p(y|x)].

        Parameters
        ----------
        src : array, shape (`order of spectral feature`)
            source speaker's spectral feature that will be transformed

        Return
        ------
        converted spectral feature
        """
        D = len(src)

        # Eq.(11)
        E = np.zeros((self.num_mixtures, D))
        for m in range(self.num_mixtures):
            xx = np.linalg.solve(self.covarXX[m], src - self.src_means[m])
            E[m] = self.tgt_means[m] + self.covarYX[m].dot(xx)

        # Eq.(9) p(m|x)
        posterior = self.px.predict_proba(np.atleast_2d(src))

        # Eq.(13) conditinal mean E[p(y|x)]
        return posterior.dot(E)


class TrajectoryGMMMap(GMMMap):

    """
    Trajectory-based speech parameter mapping for voice conversion
    based on the maximum likelihood criterion.

    Parameters
    ----------
    gmm : sklearn.mixture.GMM
        Gaussian Mixture Models of source and target speaker joint features

    gv : sklearn.mixture.GMM (default=None)
        Gaussian Mixture Models of target speaker's global variance of spectral
        feature

    swap : bool (default=False)
        True: source -> target
        False target -> source

    Attributes
    ----------
    TODO

    Reference
    ---------
      - [Toda 2007] Voice Conversion Based on Maximum Likelihood Estimation
        of Spectral Parameter Trajectory.
        http://isw3.naist.jp/~tomoki/Tomoki/Journals/IEEE-Nov-2007_MLVC.pdf
    """

    def __init__(self, gmm, T, gv=None, swap=False):
        GMMMap.__init__(self, gmm, swap)

        self.T = T
        # shape[1] = d(src) + d(src_delta) + d(tgt) + d(tgt_delta)
        D = gmm.means_.shape[1] / 4

        # Setup for Trajectory-based mapping
        self.__construct_weight_matrix(T, D)

        # Setup for GV post-filtering
        # It is assumed that GV is modeled as a single mixture GMM
        if gv != None:
            self.gv_mean = gv.means_[0]
            self.gv_covar = gv.covars_[0]
            self.Pv = np.linalg.inv(self.gv_covar)

    def __construct_weight_matrix(self, T, D):
        # Construct Weight matrix W
        # Eq.(25) ~ (28)
        for t in range(T):
            w0 = scipy.sparse.lil_matrix((D, D * T))
            w1 = scipy.sparse.lil_matrix((D, D * T))
            w0[0:, t * D:(t + 1) * D] = scipy.sparse.diags(np.ones(D), 0)

            if t - 1 >= 0:
                tmp = np.zeros(D)
                tmp.fill(-0.5)
                w1[0:, (t - 1) * D:t * D] = scipy.sparse.diags(tmp, 0)
            if t + 1 < T:
                tmp = np.zeros(D)
                tmp.fill(0.5)
                w1[0:, (t + 1) * D:(t + 2) * D] = scipy.sparse.diags(tmp, 0)

            W_t = scipy.sparse.vstack([w0, w1])

            # Slower
            # self.W[2*D*t:2*D*(t+1),:] = W_t

            if t == 0:
                self.W = W_t
            else:
                self.W = scipy.sparse.vstack([self.W, W_t])

        self.W = scipy.sparse.csr_matrix(self.W)

        assert self.W.shape == (2 * D * T, D * T)

    def convert(self, src):
        """
        Mapping source spectral feature x to target spectral feature y
        so that maximize the likelihood of y given x.

        Parameters
        ----------
        src : array, shape (`the number of frames`, `the order of spectral feature`)
            a sequence of source speaker's spectral feature that will be
            transformed

        Return
        ------
        a sequence of transformed spectral features
        """
        T, D = src.shape[0], src.shape[1] / 2

        if T != self.T:
            self.__construct_weight_matrix(T, D)

        # A suboptimum mixture sequence  (eq.37)
        optimum_mix = self.px.predict(src)

        # Compute E eq.(40)
        self.E = np.zeros((T, 2 * D))
        for t in range(T):
            m = optimum_mix[t]  # estimated mixture index at time t
            xx = np.linalg.solve(self.covarXX[m], src[t] - self.src_means[m])
            # Eq. (22)
            self.E[t] = self.tgt_means[m] + np.dot(self.covarYX[m], xx)
        self.E = self.E.flatten()

        # Compute D eq.(41). Note that self.D represents D^-1.
        self.D = np.zeros((T, 2 * D, 2 * D))
        for t in range(T):
            m = optimum_mix[t]
            xx_inv_xy = np.linalg.solve(self.covarXX[m], self.covarXY[m])
            # Eq. (23)
            self.D[t] = self.covarYY[m] - np.dot(self.covarYX[m], xx_inv_xy)
            self.D[t] = np.linalg.inv(self.D[t])
        self.D = scipy.sparse.block_diag(self.D, format="csr")

        # Compute target static features
        # eq.(39)
        covar = self.W.T.dot(self.D.dot(self.W))
        y = scipy.sparse.linalg.spsolve(covar, self.W.T.dot(self.D.dot(self.E)),
                                        use_umfpack=False)
        return y.reshape((T, D))

    def convert_with_gv(self, src, epoches=1000, learning_rate=0.0045):
        """
        Mapping source spectral feature x to target spectral feature y
        so that maximize the likelihood of y given x with considering
        global variance.

        Parameters
        ----------
        src : array, shape (`the number of frames`, `the order of spectral feature`)
            a sequence of source speaker's spectral feature that will be
            transformed

        epoches : int
            number of iterations in gradient decent

        Return
        ------
        transformed spectral feature
        """
        # Initialize target static features without considering GV
        y = self.convert(src)

        T, D = y.shape
        omega = 1.0 / (2.0 * T)

        # Gradient decent
        for i in range(epoches):
            # Eq.(57)
            grad_y = -self.W.T.dot(self.D.dot(self.W.dot(y.flatten()))) \
                + self.W.T.dot(self.D.dot(self.E))
            grad = omega * grad_y + self.__gv_grad(y).flatten()
            print i, np.linalg.norm(grad)
            y = y + learning_rate * grad.reshape((T, D))

        return y

    def __gv_grad(self, y):
        """
        Compute gradient of the likelihood with regard to GV

        Parameters
        ----------
        y : array, shape (`the number of frames`, `the order of spectral feature`)
            current estimate of a sequence of target static spectral feature

        Return
        ------
        gradient of the likelihood with regard to global variance
        """
        T, D = y.shape

        gv = np.var(y, axis=0)  # global variance over time
        y_mean = np.mean(y, axis=0)
        v = np.zeros((T, D))

        # Eq.(55)
        for t in range(T):
            v[t] = -2.0 / T * \
                np.dot(self.Pv.T, (gv - self.gv_mean)) * (y[t] - y_mean)

        return v
