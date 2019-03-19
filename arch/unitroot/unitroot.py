from __future__ import absolute_import, division

from arch.compat.python import add_metaclass, lmap, long, range

import warnings

from numpy import (abs, arange, argwhere, array, ceil, cumsum, diff, empty,
                   float64, hstack, inf, int32, int64, interp, log, nan, ones,
                   pi, polyval, power, sort, sqrt, squeeze, sum, amin)
from numpy.linalg import inv, matrix_rank, pinv, qr, solve
from pandas import DataFrame
from scipy.stats import norm
from statsmodels.iolib.summary import Summary
from statsmodels.iolib.table import SimpleTable
from statsmodels.regression.linear_model import OLS
from statsmodels.tsa.tsatools import lagmat

from arch.unitroot.critical_values.dfgls import (dfgls_cv_approx,
                                                 dfgls_large_p, dfgls_small_p,
                                                 dfgls_tau_max, dfgls_tau_min,
                                                 dfgls_tau_star)
from arch.unitroot.critical_values.dickey_fuller import (adf_z_cv_approx,
                                                         adf_z_large_p,
                                                         adf_z_max, adf_z_min,
                                                         adf_z_small_p,
                                                         adf_z_star, tau_2010,
                                                         tau_large_p, tau_max,
                                                         tau_min, tau_small_p,
                                                         tau_star)
from arch.unitroot.critical_values.kpss import kpss_critical_values
from arch.utility import cov_nw
from arch.utility.array import DocStringInheritor, ensure1d
from arch.utility.exceptions import InvalidLengthWarning, invalid_length_doc
from arch.utility.timeseries import add_trend

__all__ = ['ADF', 'DFGLS', 'PhillipsPerron', 'KPSS', 'VarianceRatio',
           'kpss_crit', 'mackinnoncrit', 'mackinnonp']

TREND_MAP = {None: 'nc', 0: 'c', 1: 'ct', 2: 'ctt'}

TREND_DESCRIPTION = {'nc': 'No Trend',
                     'c': 'Constant',
                     'ct': 'Constant and Linear Time Trend',
                     'ctt': 'Constant, Linear and Quadratic Time Trends'}


def _select_best_ic(method, nobs, sigma2, tstat):
    """
    Comutes the best information criteria

    Parameters
    ----------
    method : {'aic', 'bic', 't-stat'}
        Method to use when finding the lag length
    nobs : int
        Number of observations in time series
    sigma2 : ndarray
        maxlag + 1 array containins MLE estimates of the residual variance
    tstat : ndarray
        maxlag + 1 array containing t-statistic values. Only used if method
        is 't-stat'

    Returns
    -------
    icbest : float
        Minimum value of the information criteria
    lag : int
        The lag length that maximizes the information criterion.
    """
    llf = -nobs / 2.0 * (log(2 * pi) + log(sigma2) + 1)
    maxlag = len(sigma2) - 1
    if method == 'aic':
        crit = -2 * llf + 2 * arange(float(maxlag + 1))
        icbest, lag = min(zip(crit, arange(maxlag + 1)))
    elif method == 'bic':
        crit = -2 * llf + log(nobs) * arange(float(maxlag + 1))
        icbest, lag = min(zip(crit, arange(maxlag + 1)))
    elif method == 't-stat':
        stop = 1.6448536269514722
        large_tstat = abs(tstat) >= stop
        lag = int(squeeze(max(argwhere(large_tstat))))
        icbest = float(tstat[lag])
    else:
        raise ValueError('Unknown method')

    return icbest, lag


def _autolag_ols_low_memory(y, maxlag, trend, method):
    """
    Compules the lag length that minimizes an info criterion .

    Parameters
    ----------
    y : ndarray
        Variable being tested for a unit root
    maxlag : int
        The highest lag order for lag length selection.
    trend : {'nc', 'c', 'ct','ctt'}
        Trend in the model
    method : {'aic', 'bic', 't-stat'}
        Method to use when finding the lag length

    Returns
    -------
    icbest : float
        Minimum value of the information criteria
    lag : int
        The lag length that maximizes the information criterion.

    Notes
    -----
    Minimizes creation of large arrays. Uses approx 6 * nobs temporary values
    """
    method = method.lower()
    deltay = diff(y)
    deltay = deltay / sqrt(deltay.dot(deltay))
    lhs = deltay[maxlag:][:, None]
    level = y[maxlag:-1]
    level = level / sqrt(level.dot(level))
    trendx = []
    nobs = lhs.shape[0]
    if trend == 'nc':
        trendx = empty((nobs, 0))
    else:
        if 'tt' in trend:
            tt = arange(1, nobs + 1, dtype=float64)[:, None] ** 2
            tt *= (sqrt(5) / float(nobs) ** (5 / 2))
            trendx.append(tt)
        if 't' in trend:
            t = arange(1, nobs + 1, dtype=float64)[:, None]
            t *= (sqrt(3) / float(nobs) ** (3 / 2))
            trendx.append(t)
        if trend.startswith('c'):
            trendx.append(ones((nobs, 1)) / sqrt(nobs))
        trendx = hstack(trendx)
    rhs = hstack([level[:, None], trendx])
    m = rhs.shape[1]
    xpx = empty((m + maxlag, m + maxlag)) * nan
    xpy = empty((m + maxlag, 1)) * nan
    xpy[:m] = rhs.T.dot(lhs)
    xpx[:m, :m] = rhs.T.dot(rhs)
    for i in range(maxlag):
        x1 = deltay[maxlag - i - 1:-(1 + i)]
        block = rhs.T.dot(x1)
        xpx[m + i, :m] = block
        xpx[:m, m + i] = block
        xpy[m + i] = x1.dot(lhs)
        for j in range(i, maxlag):
            x2 = deltay[maxlag - j - 1:-(1 + j)]
            x1px2 = x1.dot(x2)
            xpx[m + i, m + j] = x1px2
            xpx[m + j, m + i] = x1px2
    ypy = lhs.T.dot(lhs)
    sigma2 = empty(maxlag + 1)

    tstat = empty(maxlag + 1)
    tstat[0] = inf
    for i in range(m, m + maxlag + 1):
        xpx_sub = xpx[:i, :i]
        b = solve(xpx_sub, xpy[:i])
        sigma2[i - m] = (ypy - b.T.dot(xpx_sub).dot(b)) / nobs
        if method == 't-stat':
            xpxi = inv(xpx_sub)
            stderr = sqrt(sigma2[i - m] * xpxi[-1, -1])
            tstat[i - m] = b[-1] / stderr

    return _select_best_ic(method, nobs, sigma2, tstat)


def _autolag_ols(endog, exog, startlag, maxlag, method):
    """
    Returns the results for the lag length that maximizes the info criterion.

    Parameters
    ----------
    endog : {ndarray, Series}
        nobs array containing endogenous variable
    exog : {ndarray, DataFrame}
        nobs by (startlag + maxlag) array containing lags and possibly other
        variables
    startlag : int
        The first zero-indexed column to hold a lag.  See Notes.
    maxlag : int
        The highest lag order for lag length selection.
    method : {'aic', 'bic', 't-stat'}
        aic - Akaike Information Criterion
        bic - Bayes Information Criterion
        t-stat - Based on last lag

    Returns
    -------
    icbest : float
        Minimum value of the information criteria
    lag : int
        The lag length that maximizes the information criterion.

    Notes
    -----
    Does estimation like mod(endog, exog[:,:i]).fit()
    where i goes from lagstart to lagstart + maxlag + 1.  Therefore, lags are
    assumed to be in contiguous columns from low to high lag length with
    the highest lag in the last column.
    """
    method = method.lower()

    q, r = qr(exog)
    qpy = q.T.dot(endog)
    ypy = endog.T.dot(endog)
    xpx = exog.T.dot(exog)
    effective_max_lag = min(maxlag, matrix_rank(xpx) - startlag)

    sigma2 = empty(effective_max_lag + 1)
    tstat = empty(effective_max_lag + 1)
    nobs = float(endog.shape[0])
    tstat[0] = inf
    for i in range(startlag, startlag + effective_max_lag + 1):
        b = solve(r[:i, :i], qpy[:i])
        sigma2[i - startlag] = (ypy - b.T.dot(xpx[:i, :i]).dot(b)) / nobs
        if method == 't-stat' and i > startlag:
            xpxi = inv(xpx[:i, :i])
            stderr = sqrt(sigma2[i - startlag] * xpxi[-1, -1])
            tstat[i - startlag] = b[-1] / stderr

    return _select_best_ic(method, nobs, sigma2, tstat)


def _df_select_lags(y, trend, max_lags, method, low_memory=False):
    """
    Helper method to determine the best lag length in DF-like regressions

    Parameters
    ----------
    y : ndarray
        The data for the lag selection exercise
    trend : {'nc','c','ct','ctt'}
        The trend order
    max_lags : int
        The maximum number of lags to check.  This setting affects all
        estimation since the sample is adjusted by max_lags when
        fitting the models
    method : {'AIC','BIC','t-stat'}
        The method to use when estimating the model
    low_memory : bool
        Flag indicating whether to use the low-memory algorithm for
        lag-length selection.

    Returns
    -------
    best_ic : float
        The information criteria at the selected lag
    best_lag : int
        The selected lag

    Notes
    -----
    If max_lags is None, the default value of 12 * (nobs/100)**(1/4) is used.
    """
    nobs = y.shape[0]
    # This is the absolute maximum number of lags possible,
    # only needed to very short time series.
    max_max_lags = nobs // 2 - 1
    if trend != 'nc':
        max_max_lags -= len(trend)
    if max_lags is None:
        max_lags = int(ceil(12. * power(nobs / 100., 1 / 4.)))
        max_lags = max(min(max_lags, max_max_lags), 0)
    if low_memory:
        out = _autolag_ols_low_memory(y, max_lags, trend, method)
        return out

    delta_y = diff(y)
    rhs = lagmat(delta_y[:, None], max_lags, trim='both', original='in')
    nobs = rhs.shape[0]
    rhs[:, 0] = y[-nobs - 1:-1]  # replace 0 with level of y
    lhs = delta_y[-nobs:]

    if trend != 'nc':
        full_rhs = add_trend(rhs, trend, prepend=True)
    else:
        full_rhs = rhs

    start_lag = full_rhs.shape[1] - rhs.shape[1] + 1
    ic_best, best_lag = _autolag_ols(lhs, full_rhs, start_lag, max_lags, method)
    return ic_best, best_lag


def _add_column_names(rhs, lags):
    """Return a DataFrame with named columns"""
    lag_names = ['Diff.L{0}'.format(i) for i in range(1, lags + 1)]
    return DataFrame(rhs, columns=['Level.L1'] + lag_names)


def _estimate_df_regression(y, trend, lags):
    """Helper function that estimates the core (A)DF regression

    Parameters
    ----------
    y : ndarray
        The data for the lag selection
    trend : {'nc','c','ct','ctt'}
        The trend order
    lags : int
        The number of lags to include in the ADF regression

    Returns
    -------
    ols_res : OLSResults
        A results class object produced by OLS.fit()

    Notes
    -----
    See statsmodels.regression.linear_model.OLS for details on the results
    returned
    """
    delta_y = diff(y)

    rhs = lagmat(delta_y[:, None], lags, trim='both', original='in')
    nobs = rhs.shape[0]
    lhs = rhs[:, 0].copy()  # lag-0 values are lhs, Is copy() necessary?
    rhs[:, 0] = y[-nobs - 1:-1]  # replace lag 0 with level of y
    rhs = _add_column_names(rhs, lags)

    if trend != 'nc':
        rhs = add_trend(rhs.iloc[:, :lags + 1], trend)

    return OLS(lhs, rhs).fit()


class UnitRootTest(object):
    """Base class to be used for inheritance in unit root bootstrap"""

    def __init__(self, y, lags, trend, valid_trends):
        self._y = ensure1d(y, 'y')
        self._delta_y = diff(y)
        self._nobs = self._y.shape[0]
        self._lags = None
        self.lags = lags
        self._valid_trends = valid_trends
        self._trend = ''
        self.trend = trend
        self._stat = None
        self._critical_values = None
        self._pvalue = None
        self.trend = trend
        self._null_hypothesis = 'The process contains a unit root.'
        self._alternative_hypothesis = 'The process is weakly stationary.'
        self._test_name = None
        self._title = None
        self._summary_text = None

    def __str__(self):
        return self.summary().__str__()

    def __repr__(self):
        return str(type(self)) + '\n"""\n' + self.__str__() + '\n"""'

    def _repr_html_(self):
        """Display as HTML for IPython notebook.
        """
        return self.summary().as_html()

    def _compute_statistic(self):
        """This is the core routine that computes the test statistic, computes
        the p-value and constructs the critical values.
        """
        raise NotImplementedError("Subclass must implement")

    def _reset(self):
        """Resets the unit root test so that it will be recomputed
        """
        self._stat = None
        assert self._stat is None

    def _compute_if_needed(self):
        """Checks whether the statistic needs to be computed, and computed if
        needed
        """
        if self._stat is None:
            self._compute_statistic()

    @property
    def null_hypothesis(self):
        """The null hypothesis
        """
        return self._null_hypothesis

    @property
    def alternative_hypothesis(self):
        """The alternative hypothesis
        """
        return self._alternative_hypothesis

    @property
    def nobs(self):
        """The number of observations used when computing the test statistic.
        Accounts for loss of data due to lags for regression-based bootstrap."""
        return self._nobs

    @property
    def valid_trends(self):
        """List of valid trend terms."""
        return self._valid_trends

    @property
    def pvalue(self):
        """Returns the p-value for the test statistic
        """
        self._compute_if_needed()
        return self._pvalue

    @property
    def stat(self):
        """The test statistic for a unit root
        """
        self._compute_if_needed()
        return self._stat

    @property
    def critical_values(self):
        """Dictionary containing critical values specific to the test, number of
        observations and included deterministic trend terms.
        """
        self._compute_if_needed()
        return self._critical_values

    def summary(self):
        """Summary of test, containing statistic, p-value and critical values
        """
        table_data = [('Test Statistic', '{0:0.3f}'.format(self.stat)),
                      ('P-value', '{0:0.3f}'.format(self.pvalue)),
                      ('Lags', '{0:d}'.format(self.lags))]
        title = self._title

        if not title:
            title = self._test_name + " Results"
        table = SimpleTable(table_data, stubs=None, title=title, colwidths=18,
                            datatypes=[0, 1], data_aligns=("l", "r"))

        smry = Summary()
        smry.tables.append(table)

        cv_string = 'Critical Values: '
        cv = self._critical_values.keys()
        cv_numeric = array(lmap(lambda x: float(x.split('%')[0]), cv))
        cv_numeric = sort(cv_numeric)
        for val in cv_numeric:
            p = str(int(val)) + '%'
            cv_string += '{0:0.2f}'.format(self._critical_values[p])
            cv_string += ' (' + p + ')'
            if val != cv_numeric[-1]:
                cv_string += ', '

        extra_text = ['Trend: ' + TREND_DESCRIPTION[self._trend],
                      cv_string,
                      'Null Hypothesis: ' + self.null_hypothesis,
                      'Alternative Hypothesis: ' + self.alternative_hypothesis]

        smry.add_extra_txt(extra_text)
        if self._summary_text:
            smry.add_extra_txt(self._summary_text)
        return smry

    @property
    def lags(self):
        """Sets or gets the number of lags used in the model.
        When bootstrap use DF-type regressions, lags is the number of lags in the
        regression model.  When bootstrap use long-run variance estimators, lags
        is the number of lags used in the long-run variance estimator.
        """
        self._compute_if_needed()
        return self._lags

    @lags.setter
    def lags(self, value):
        types = (int, long, int32, int64)
        if value is not None and not isinstance(value, types) or \
                (isinstance(value, types) and value < 0):
            raise ValueError('lags must be a non-negative integer or None')
        if self._lags != value:
            self._reset()
        self._lags = value

    @property
    def y(self):
        """Returns the data used in the test statistic
        """
        return self._y

    @property
    def trend(self):
        """Sets or gets the deterministic trend term used in the test. See
        valid_trends for a list of supported trends
        """
        return self._trend

    @trend.setter
    def trend(self, value):
        if value not in self.valid_trends:
            raise ValueError('trend not understood')
        if self._trend != value:
            self._reset()
            self._trend = value


@add_metaclass(DocStringInheritor)
class ADF(UnitRootTest):
    """
    Augmented Dickey-Fuller unit root test

    Parameters
    ----------
    y : {ndarray, Series}
        The data to test for a unit root
    lags : int, optional
        The number of lags to use in the ADF regression.  If omitted or None,
        `method` is used to automatically select the lag length with no more
        than `max_lags` are included.
    trend : {'nc', 'c', 'ct', 'ctt'}, optional
        The trend component to include in the ADF test
        'nc' - No trend components
        'c' - Include a constant (Default)
        'ct' - Include a constant and linear time trend
        'ctt' - Include a constant and linear and quadratic time trends
    max_lags : int, optional
        The maximum number of lags to use when selecting lag length
    method : {'AIC', 'BIC', 't-stat'}, optional
        The method to use when selecting the lag length
        'AIC' - Select the minimum of the Akaike IC
        'BIC' - Select the minimum of the Schwarz/Bayesian IC
        't-stat' - Select the minimum of the Schwarz/Bayesian IC
    low_memory : bool
        Flag indicating whether to use a low memory implementation of the
        lag selection algorithm. The low memory algorithm is slower than
        the standard algorithm but will use 2-4% of the memory required for
        the standard algorithm. This options allows automatic lag selection
        to be used in very long time series. If None, use automatic selection
        of algorithm.

    Attributes
    ----------
    stat
    pvalue
    critical_values
    null_hypothesis
    alternative_hypothesis
    summary
    regression
    valid_trends
    y
    trend
    lags

    Notes
    -----
    The null hypothesis of the Augmented Dickey-Fuller is that there is a unit
    root, with the alternative that there is no unit root. If the pvalue is
    above a critical size, then the null cannot be rejected that there
    and the series appears to be a unit root.

    The p-values are obtained through regression surface approximation from
    MacKinnon (1994) using the updated 2010 tables.
    If the p-value is close to significant, then the critical values should be
    used to judge whether to reject the null.

    The autolag option and maxlag for it are described in Greene.

    Examples
    --------
    >>> from arch.unitroot import ADF
    >>> import numpy as np
    >>> import statsmodels.api as sm
    >>> data = sm.datasets.macrodata.load().data
    >>> inflation = np.diff(np.log(data['cpi']))
    >>> adf = ADF(inflation)
    >>> print('{0:0.4f}'.format(adf.stat))
    -3.0931
    >>> print('{0:0.4f}'.format(adf.pvalue))
    0.0271
    >>> adf.lags
    2
    >>> adf.trend='ct'
    >>> print('{0:0.4f}'.format(adf.stat))
    -3.2111
    >>> print('{0:0.4f}'.format(adf.pvalue))
    0.0822

    References
    ----------
    Greene, W. H. 2011. Econometric Analysis. Prentice Hall: Upper Saddle
    River, New Jersey.

    Hamilton, J. D. 1994. Time Series Analysis. Princeton: Princeton
    University Press.

    P-Values (regression surface approximation)
    MacKinnon, J.G. 1994.  "Approximate asymptotic distribution functions for
    unit-root and cointegration bootstrap.  `Journal of Business and Economic
    Statistics` 12, 167-76.

    Critical values
    MacKinnon, J.G. 2010. "Critical Values for Cointegration Tests."  Queen's
    University, Dept of Economics, Working Papers.  Available at
    http://ideas.repec.org/p/qed/wpaper/1227.html
    """

    def __init__(self, y, lags=None, trend='c',
                 max_lags=None, method='AIC', low_memory=None):
        valid_trends = ('nc', 'c', 'ct', 'ctt')
        super(ADF, self).__init__(y, lags, trend, valid_trends)
        self._max_lags = max_lags
        self._method = method
        self._test_name = 'Augmented Dickey-Fuller'
        self._regression = None
        self._summary_text = None
        self._low_memory = low_memory
        if low_memory is None:
            self._low_memory = True if self.y.shape[0] > 1e5 else False

    def _select_lag(self):
        ic_best, best_lag = _df_select_lags(self._y, self._trend,
                                            self._max_lags, self._method,
                                            low_memory=self._low_memory)
        self._ic_best = ic_best
        self._lags = best_lag

    def _compute_statistic(self):
        if self._lags is None:
            self._select_lag()
        y, trend, lags = self._y, self._trend, self._lags
        resols = _estimate_df_regression(y, trend, lags)
        self._regression = resols
        self._stat = stat = resols.tvalues[0]
        self._nobs = int(resols.nobs)
        self._pvalue = mackinnonp(stat, regression=trend,
                                  num_unit_roots=1)
        critical_values = mackinnoncrit(num_unit_roots=1,
                                        regression=trend,
                                        nobs=resols.nobs)
        self._critical_values = {"1%": critical_values[0],
                                 "5%": critical_values[1],
                                 "10%": critical_values[2]}

    @property
    def regression(self):
        """Returns the OLS regression results from the ADF model estimated
        """
        self._compute_if_needed()
        return self._regression

    @property
    def max_lags(self):
        """Sets or gets the maximum lags used when automatically selecting lag
        length"""
        return self._max_lags

    @max_lags.setter
    def max_lags(self, value):
        if self._max_lags != value:
            self._reset()
            self._lags = None
        self._max_lags = value


@add_metaclass(DocStringInheritor)
class DFGLS(UnitRootTest):
    """
    Elliott, Rothenberg and Stock's GLS version of the Dickey-Fuller test

    Parameters
    ----------
    y : {ndarray, Series}
        The data to test for a unit root
    lags : int, optional
        The number of lags to use in the ADF regression.  If omitted or None,
        `method` is used to automatically select the lag length with no more
        than `max_lags` are included.
    trend : {'c', 'ct'}, optional
        The trend component to include in the ADF test
        'c' - Include a constant (Default)
        'ct' - Include a constant and linear time trend
    max_lags : int, optional
        The maximum number of lags to use when selecting lag length
    method : {'AIC', 'BIC', 't-stat'}, optional
        The method to use when selecting the lag length
        'AIC' - Select the minimum of the Akaike IC
        'BIC' - Select the minimum of the Schwarz/Bayesian IC
        't-stat' - Select the minimum of the Schwarz/Bayesian IC

    Attributes
    ----------
    stat
    pvalue
    critical_values
    null_hypothesis
    alternative_hypothesis
    summary
    regression
    valid_trends
    y
    trend
    lags

    Notes
    -----
    The null hypothesis of the Dickey-Fuller GLS is that there is a unit
    root, with the alternative that there is no unit root. If the pvalue is
    above a critical size, then the null cannot be rejected and the series
    appears to be a unit root.

    DFGLS differs from the ADF test in that an initial GLS detrending step
    is used before a trend-less ADF regression is run.

    Critical values and p-values when trend is 'c' are identical to
    the ADF.  When trend is set to 'ct, they are from ...

    Examples
    --------
    >>> from arch.unitroot import DFGLS
    >>> import numpy as np
    >>> import statsmodels.api as sm
    >>> data = sm.datasets.macrodata.load().data
    >>> inflation = np.diff(np.log(data['cpi']))
    >>> dfgls = DFGLS(inflation)
    >>> print('{0:0.4f}'.format(dfgls.stat))
    -2.7611
    >>> print('{0:0.4f}'.format(dfgls.pvalue))
    0.0059
    >>> dfgls.lags
    2
    >>> dfgls.trend = 'ct'
    >>> print('{0:0.4f}'.format(dfgls.stat))
    -2.9036
    >>> print('{0:0.4f}'.format(dfgls.pvalue))
    0.0447

    References
    ----------
    Elliott, G. R., T. J. Rothenberg, and J. H. Stock. 1996. Efficient bootstrap
    for an autoregressive unit root. Econometrica 64: 813-836
    """

    def __init__(self, y, lags=None, trend='c',
                 max_lags=None, method='AIC', low_memory=None):
        valid_trends = ('c', 'ct')
        super(DFGLS, self).__init__(y, lags, trend, valid_trends)
        self._max_lags = max_lags
        self._method = method
        self._regression = None
        self._low_memory = low_memory
        if low_memory is None:
            self._low_memory = True if self.y.shape[0] >= 1e5 else False
        self._test_name = 'Dickey-Fuller GLS'
        if trend == 'c':
            self._c = -7.0
        else:
            self._c = -13.5

    def _compute_statistic(self):
        """Core routine to estimate DF-GLS test statistic"""
        # 1. GLS detrend
        trend, c = self._trend, self._c

        nobs = self._y.shape[0]
        ct = c / nobs
        z = add_trend(nobs=nobs, trend=trend)

        delta_z = z.copy()
        delta_z[1:, :] = delta_z[1:, :] - (1 + ct) * delta_z[:-1, :]
        delta_y = self._y.copy()[:, None]
        delta_y[1:] = delta_y[1:] - (1 + ct) * delta_y[:-1]
        detrend_coef = pinv(delta_z).dot(delta_y)
        y = self._y
        y_detrended = y - z.dot(detrend_coef).ravel()

        # 2. determine lag length, if needed
        if self._lags is None:
            max_lags, method = self._max_lags, self._method
            icbest, bestlag = _df_select_lags(y_detrended, 'nc', max_lags, method,
                                              low_memory=self._low_memory)
            self._lags = bestlag

        # 3. Run Regression
        lags = self._lags

        resols = _estimate_df_regression(y_detrended,
                                         lags=lags,
                                         trend='nc')
        self._regression = resols
        self._nobs = int(resols.nobs)
        self._stat = resols.tvalues[0]
        self._pvalue = mackinnonp(self._stat,
                                  regression=trend,
                                  dist_type='DFGLS')
        critical_values = mackinnoncrit(regression=trend,
                                        nobs=self._nobs,
                                        dist_type='DFGLS')
        self._critical_values = {"1%": critical_values[0],
                                 "5%": critical_values[1],
                                 "10%": critical_values[2]}

    @UnitRootTest.trend.setter
    def trend(self, value):
        if value not in self.valid_trends:
            raise ValueError('trend not understood')
        if self._trend != value:
            self._reset()
            self._trend = value
        if value == 'c':
            self._c = -7.0
        else:
            self._c = -13.5

    @property
    def regression(self):
        """Returns the OLS regression results from the ADF model estimated
        """
        self._compute_if_needed()
        return self._regression

    @property
    def max_lags(self):
        """Sets or gets the maximum lags used when automatically selecting lag
        length"""
        return self._max_lags

    @max_lags.setter
    def max_lags(self, value):
        if self._max_lags != value:
            self._reset()
            self._lags = None
        self._max_lags = value


@add_metaclass(DocStringInheritor)
class PhillipsPerron(UnitRootTest):
    """
    Phillips-Perron unit root test

    Parameters
    ----------
    y : {ndarray, Series}
        The data to test for a unit root
    lags : int, optional
        The number of lags to use in the Newey-West estimator of the long-run
        covariance.  If omitted or None, the lag length is set automatically to
        12 * (nobs/100) ** (1/4)
    trend : {'nc', 'c', 'ct'}, optional
        The trend component to include in the ADF test
            'nc' - No trend components
            'c' - Include a constant (Default)
            'ct' - Include a constant and linear time trend
    test_type : {'tau', 'rho'}
        The test to use when computing the test statistic. 'tau' is based on
        the t-stat and 'rho' uses a test based on nobs times the re-centered
        regression coefficient

    Attributes
    ----------
    stat
    pvalue
    critical_values
    test_type
    null_hypothesis
    alternative_hypothesis
    summary
    valid_trends
    y
    trend
    lags

    Notes
    -----
    The null hypothesis of the Phillips-Perron (PP) test is that there is a
    unit root, with the alternative that there is no unit root. If the pvalue
    is above a critical size, then the null cannot be rejected that there
    and the series appears to be a unit root.

    Unlike the ADF test, the regression estimated includes only one lag of
    the dependant variable, in addition to trend terms. Any serial
    correlation in the regression errors is accounted for using a long-run
    variance estimator (currently Newey-West).

    The p-values are obtained through regression surface approximation from
    MacKinnon (1994) using the updated 2010 tables.
    If the p-value is close to significant, then the critical values should be
    used to judge whether to reject the null.

    Examples
    --------
    >>> from arch.unitroot import PhillipsPerron
    >>> import numpy as np
    >>> import statsmodels.api as sm
    >>> data = sm.datasets.macrodata.load().data
    >>> inflation = np.diff(np.log(data['cpi']))
    >>> pp = PhillipsPerron(inflation)
    >>> print('{0:0.4f}'.format(pp.stat))
    -8.1356
    >>> print('{0:0.4f}'.format(pp.pvalue))
    0.0000
    >>> pp.lags
    15
    >>> pp.trend = 'ct'
    >>> print('{0:0.4f}'.format(pp.stat))
    -8.2022
    >>> print('{0:0.4f}'.format(pp.pvalue))
    0.0000
    >>> pp.test_type = 'rho'
    >>> print('{0:0.4f}'.format(pp.stat))
    -120.3271
    >>> print('{0:0.4f}'.format(pp.pvalue))
    0.0000

    References
    ----------
    Hamilton, J. D. 1994. Time Series Analysis. Princeton: Princeton
    University Press.

    Newey, W. K., and K. D. West. 1987. A simple, positive semidefinite,
    heteroskedasticity and autocorrelation consistent covariance matrix.
    Econometrica 55, 703-708.

    Phillips, P. C. B., and P. Perron. 1988. Testing for a unit root in
    time series regression. Biometrika 75, 335-346.

    P-Values (regression surface approximation)
    MacKinnon, J.G. 1994.  "Approximate asymptotic distribution functions for
    unit-root and cointegration bootstrap.  `Journal of Business and Economic
    Statistics` 12, 167-76.

    Critical values
    MacKinnon, J.G. 2010. "Critical Values for Cointegration Tests."  Queen's
    University, Dept of Economics, Working Papers.  Available at
    http://ideas.repec.org/p/qed/wpaper/1227.html
    """

    def __init__(self, y, lags=None, trend='c', test_type='tau'):
        valid_trends = ('nc', 'c', 'ct')
        super(PhillipsPerron, self).__init__(y, lags, trend, valid_trends)
        self._test_type = test_type
        self._stat_rho = None
        self._stat_tau = None
        self._test_name = 'Phillips-Perron Test'
        self._lags = lags

    def _compute_statistic(self):
        """Core routine to estimate PP test statistics"""
        # 1. Estimate Regression
        y, trend = self._y, self._trend
        nobs = y.shape[0]

        if self._lags is None:
            self._lags = int(ceil(12. * power(nobs / 100., 1 / 4.)))
        lags = self._lags

        rhs = y[:-1, None]
        rhs = _add_column_names(rhs, 0)
        lhs = y[1:, None]
        if trend != 'nc':
            rhs = add_trend(rhs, trend)

        resols = OLS(lhs, rhs).fit()
        k = rhs.shape[1]
        n, u = resols.nobs, resols.resid
        lam2 = cov_nw(u, lags, demean=False)
        lam = sqrt(lam2)
        # 2. Compute components
        s2 = u.dot(u) / (n - k)
        s = sqrt(s2)
        gamma0 = s2 * (n - k) / n
        sigma = resols.bse[0]
        sigma2 = sigma ** 2.0
        rho = resols.params[0]
        # 3. Compute statistics
        self._stat_tau = sqrt(gamma0 / lam2) * ((rho - 1) / sigma) \
            - 0.5 * ((lam2 - gamma0) / lam) * (n * sigma / s)
        self._stat_rho = n * (rho - 1) \
            - 0.5 * (n ** 2.0 * sigma2 / s2) * (lam2 - gamma0)

        self._nobs = int(resols.nobs)
        if self._test_type == 'rho':
            self._stat = self._stat_rho
            dist_type = 'ADF-z'
        else:
            self._stat = self._stat_tau
            dist_type = 'ADF-t'

        self._pvalue = mackinnonp(self._stat,
                                  regression=trend,
                                  dist_type=dist_type)
        critical_values = mackinnoncrit(regression=trend,
                                        nobs=n,
                                        dist_type=dist_type)
        self._critical_values = {"1%": critical_values[0],
                                 "5%": critical_values[1],
                                 "10%": critical_values[2]}

        self._title = self._test_name + ' (Z-' + self._test_type + ')'

    @property
    def test_type(self):
        """Gets or sets the test type returned by stat.
        Valid values are 'tau' or 'rho'"""
        return self._test_type

    @test_type.setter
    def test_type(self, value):
        if value not in ('rho', 'tau'):
            raise ValueError('stat must be either ''rho'' or ''tau''.')
        self._reset()
        self._test_type = value


@add_metaclass(DocStringInheritor)
class KPSS(UnitRootTest):
    """
    Kwiatkowski, Phillips, Schmidt and Shin (KPSS) stationarity test

    Parameters
    ----------
    y : {ndarray, Series}
        The data to test for stationarity
    lags : int, optional
        The number of lags to use in the Newey-West estimator of the long-run
        covariance.  If omitted or None, the number of lags is calculated
        with the data-dependent method of Hobijn et al. (1998). See also
        Andrews (1991), Newey & West (1994), and Schwert (1989).
        Set lags=-1 to use the old method that only depends on the sample
        size, 12 * (nobs/100) ** (1/4).
    trend : {'c', 'ct'}, optional
        The trend component to include in the ADF test
            'c' - Include a constant (Default)
            'ct' - Include a constant and linear time trend

    Attributes
    ----------
    stat
    pvalue
    critical_values
    null_hypothesis
    alternative_hypothesis
    summary
    valid_trends
    y
    trend
    lags

    Notes
    -----
    The null hypothesis of the KPSS test is that the series is weakly
    stationary and the alternative is that it is non-stationary. If the
    p-value is above a critical size, then the null cannot be rejected
    that there and the series appears stationary.

    The p-values and critical values were computed using an extensive
    simulation based on 100,000,000 replications using series with 2,000
    observations.

    Examples
    --------
    >>> from arch.unitroot import KPSS
    >>> import numpy as np
    >>> import statsmodels.api as sm
    >>> data = sm.datasets.macrodata.load().data
    >>> inflation = np.diff(np.log(data['cpi']))
    >>> kpss = KPSS(inflation)
    >>> print('{0:0.4f}'.format(kpss.stat))
    0.2870
    >>> print('{0:0.4f}'.format(kpss.pvalue))
    0.1473
    >>> kpss.trend = 'ct'
    >>> print('{0:0.4f}'.format(kpss.stat))
    0.2075
    >>> print('{0:0.4f}'.format(kpss.pvalue))
    0.0128

    References
    ----------
    Andrews, D.W.K. (1991). Heteroskedasticity and autocorrelation consistent
    covariance matrix estimation. Econometrica, 59: 817-858.

    Hobijn, B., Frances, B.H., & Ooms, M. (2004). Generalizations of the
    KPSS-test for stationarity. Statistica Neerlandica, 52: 483-502.

    Kwiatkowski, D.; Phillips, P. C. B.; Schmidt, P.; Shin, Y. (1992). "Testing
    the null hypothesis of stationarity against the alternative of a unit
    root". Journal of Econometrics 54 (1-3), 159-178

    Newey, W.K., & West, K.D. (1994). Automatic lag selection in covariance
    matrix estimation. Review of Economic Studies, 61: 631-653.

    Schwert, G. W. (1989). Tests for unit roots: A Monte Carlo investigation.
    Journal of Business and Economic Statistics, 7 (2): 147-159.
    """

    def __init__(self, y, lags=None, trend='c'):
        valid_trends = ('c', 'ct')
        if lags is None:
            import warnings
            warnings.warn('Lag selection has changed to use a data-dependent method. To use the '
                          'old method that only depends on time, set lags=-1', DeprecationWarning)
        self._legacy_lag_selection = False
        if lags == -1:
            self._legacy_lag_selection = True
            lags = None
        super(KPSS, self).__init__(y, lags, trend, valid_trends)
        self._test_name = 'KPSS Stationarity Test'
        self._null_hypothesis = 'The process is weakly stationary.'
        self._alternative_hypothesis = 'The process contains a unit root.'
        self._resids = None

    def _compute_statistic(self):
        # 1. Estimate model with trend
        nobs, y, trend = self._nobs, self._y, self._trend
        z = add_trend(nobs=nobs, trend=trend)
        res = OLS(y, z).fit()
        # 2. Compute KPSS test
        self._resids = u = res.resid
        if self._lags is None:
            if self._legacy_lag_selection:
                self._lags = int(ceil(12. * power(nobs / 100., 1 / 4.)))
            else:
                self._autolag()
        lam = cov_nw(u, self._lags, demean=False)
        s = cumsum(u)
        self._stat = 1 / (nobs ** 2.0) * sum(s ** 2.0) / lam
        self._nobs = u.shape[0]
        self._pvalue, critical_values = kpss_crit(self._stat, trend)
        self._critical_values = {"1%": critical_values[0],
                                 "5%": critical_values[1],
                                 "10%": critical_values[2]}

    def _autolag(self):
        """
        Computes the number of lags for covariance matrix estimation in KPSS test
        using method of Hobijn et al (1998). See also Andrews (1991), Newey & West
        (1994), and Schwert (1989). Assumes Bartlett / Newey-West kernel.

        Written by Jim Varanelli
        """
        resids = self._resids
        covlags = int(power(self._nobs, 2. / 9.))
        s0 = sum(resids**2) / self._nobs
        s1 = 0
        for i in range(1, covlags + 1):
            resids_prod = resids[i:].dot(resids[:self._nobs - i])
            resids_prod /= self._nobs / 2
            s0 += resids_prod
            s1 += i * resids_prod
        s_hat = s1 / s0
        pwr = 1. / 3.
        gamma_hat = 1.1447 * power(s_hat * s_hat, pwr)
        autolags = amin([self._nobs, int(gamma_hat * power(self._nobs, pwr))])
        self._lags = autolags


@add_metaclass(DocStringInheritor)
class VarianceRatio(UnitRootTest):
    """
    Variance Ratio test of a random walk.

    Parameters
    ----------
    y : {ndarray, Series}
        The data to test for a random walk
    lags : int
        The number of periods to used in the multi-period variance, which is
        the numerator of the test statistic.  Must be at least 2
    trend : {'nc', 'c'}, optional
        'c' allows for a non-zero drift in the random walk, while 'nc' requires
        that the increments to y are mean 0
    overlap : bool, optional
        Indicates whether to use all overlapping blocks.  Default is True.  If
        False, the number of observations in y minus 1 must be an exact
        multiple of lags.  If this condition is not satisfied, some values at
        the end of y will be discarded.
    robust : bool, optional
        Indicates whether to use heteroskedasticity robust inference. Default
        is True.
    debiased : bool, optional
        Indicates whether to use a debiased version of the test. Default is
        True. Only applicable if overlap is True.

    Attributes
    ----------
    stat
    pvalue
    critical_values
    null_hypothesis
    alternative_hypothesis
    summary
    valid_trends
    y
    trend
    lags
    overlap
    robust
    debiased

    Notes
    -----
    The null hypothesis of a VR is that the process is a random walk, possibly
    plus drift.  Rejection of the null with a positive test statistic indicates
    the presence of positive serial correlation in the time series.

    Examples
    --------
    >>> from arch.unitroot import VarianceRatio
    >>> import datetime as dt
    >>> import pandas_datareader as pdr
    >>> data = pdr.get_data_fred('DJIA')
    >>> data = data.resample('M').last()  # End of month
    >>> returns = data['DJIA'].pct_change().dropna()
    >>> vr = VarianceRatio(returns, lags=12)
    >>> print('{0:0.4f}'.format(vr.pvalue))
    0.0000

    References
    ----------
    Campbell, John Y., Lo, Andrew W. and MacKinlay, A. Craig. (1997) The
    Econometrics of Financial Markets. Princeton, NJ: Princeton University
    Press.

    """

    def __init__(self, y, lags=2, trend='c', debiased=True,
                 robust=True, overlap=True):
        if lags < 2:
            raise ValueError('lags must be an integer larger than 2')
        valid_trends = ('nc', 'c')
        super(VarianceRatio, self).__init__(y, lags, trend, valid_trends)
        self._test_name = 'Variance-Ratio Test'
        self._null_hypothesis = 'The process is a random walk.'
        self._alternative_hypothesis = 'The process is not a random walk.'
        self._robust = robust
        self._debiased = debiased
        self._overlap = overlap
        self._vr = None
        self._stat_variance = None
        quantiles = array([.01, .05, .1, .9, .95, .99])
        self._critical_values = {}
        self._summary_text = ''
        for q, cv in zip(quantiles, norm.ppf(quantiles)):
            self._critical_values[str(int(100 * q)) + '%'] = cv

    @property
    def vr(self):
        """The ratio of the long block lags-period variance
        to the 1-period variance"""
        self._compute_if_needed()
        return self._vr

    @property
    def overlap(self):
        """Sets of gets the indicator to use overlapping returns in the
        long-period variance estimator"""
        return self._overlap

    @overlap.setter
    def overlap(self, value):
        self._reset()
        self._overlap = bool(value)

    @property
    def robust(self):
        """Sets of gets the indicator to use a heteroskedasticity robust
        variance estimator """
        return self._robust

    @robust.setter
    def robust(self, value):
        self._reset()
        self._robust = bool(value)

    @property
    def debiased(self):
        """Sets of gets the indicator to use debiased variances in the ratio"""
        return self._debiased

    @debiased.setter
    def debiased(self, value):
        self._reset()
        self._debiased = bool(value)

    def _compute_statistic(self):
        overlap, debiased, robust = self._overlap, self._debiased, self._robust
        y, nobs, q, trend = self._y, self._nobs, self._lags, self._trend

        nq = nobs - 1
        if not overlap:
            # Check length of y
            if nq % q != 0:
                extra = nq % q
                y = y[:-extra]
                warnings.warn(invalid_length_doc.format(var='y',
                                                        block=q,
                                                        drop=extra),
                              InvalidLengthWarning)

        nobs = y.shape[0]
        if trend == 'nc':
            mu = 0
        else:
            mu = (y[-1] - y[0]) / (nobs - 1)

        delta_y = diff(y)
        nq = delta_y.shape[0]
        sigma2_1 = sum((delta_y - mu) ** 2.0) / nq

        if not overlap:
            delta_y_q = y[q::q] - y[0:-q:q]
            sigma2_q = sum((delta_y_q - q * mu) ** 2.0) / nq
            self._summary_text = ['Computed with non-overlapping blocks']
        else:
            delta_y_q = y[q:] - y[:-q]
            sigma2_q = sum((delta_y_q - q * mu) ** 2.0) / (nq * q)
            self._summary_text = ['Computed with overlapping blocks']

        if debiased and overlap:
            sigma2_1 *= nq / (nq - 1)
            m = q * (nq - q + 1) * (1 - (q / nq))
            sigma2_q *= (nq * q) / m
            self._summary_text = ['Computed with overlapping blocks '
                                  '(de-biased)']

        if not overlap:
            self._stat_variance = 2.0 * (q - 1)
        elif not robust:
            self._stat_variance = (2 * (2 * q - 1) * (q - 1)) / (2 * q)
        else:
            z2 = (delta_y - mu) ** 2.0
            scale = sum(z2) ** 2.0
            theta = 0.0
            for k in range(1, q):
                delta = nq * z2[k:].dot(z2[:-k]) / scale
                theta += (1 - k / q) ** 2.0 * delta
            self._stat_variance = theta
        self._vr = sigma2_q / sigma2_1
        self._stat = sqrt(nq) * (self._vr - 1) / sqrt(self._stat_variance)
        self._pvalue = 2 - 2 * norm.cdf(abs(self._stat))


def mackinnonp(stat, regression="c", num_unit_roots=1, dist_type='ADF-t'):
    """
    Returns MacKinnon's approximate p-value for test stat.

    Parameters
    ----------
    stat : float
        "T-value" from an Augmented Dickey-Fuller or DFGLS regression.
    regression : {'c', 'nc', 'ct', 'ctt'}
        This is the method of regression that was used.  Following MacKinnon's
        notation, this can be "c" for constant, "nc" for no constant, "ct" for
        constant and trend, and "ctt" for constant, trend, and trend-squared.
    num_unit_roots : int
        The number of series believed to be I(1).  For (Augmented) Dickey-
        Fuller N = 1.
    dist_type : {'ADF-t', 'ADF-z', 'DFGLS'}
        The test type to use when computing p-values.  Options include
        'ADF-t' - ADF t-stat based bootstrap
        'ADF-z' - ADF z bootstrap
        'DFGLS' - GLS detrended Dickey Fuller

    Returns
    -------
    p-value : float
        The p-value for the ADF statistic estimated using MacKinnon 1994.

    References
    ----------
    MacKinnon, J.G. 1994  "Approximate Asymptotic Distribution Functions for
        Unit-Root and Cointegration Tests." Journal of Business & Economics
        Statistics, 12.2, 167-76.

    Notes
    -----
    Most values are from MacKinnon (1994).  Values for DFGLS test statistics
    and the 'nc' version of the ADF z test statistic were computed following
    the methodology of MacKinnon (1994).
    """
    dist_type = dist_type.lower()
    if num_unit_roots > 1 and dist_type.lower() != 'adf-t':
        raise ValueError('Cointegration results (num_unit_roots > 1) are' +
                         'only available for ADF-t values')
    if dist_type == 'adf-t':
        maxstat = tau_max[regression][num_unit_roots - 1]
        minstat = tau_min[regression][num_unit_roots - 1]
        starstat = tau_star[regression][num_unit_roots - 1]
        small_p = tau_small_p[regression][num_unit_roots - 1]
        large_p = tau_large_p[regression][num_unit_roots - 1]
    elif dist_type == 'adf-z':
        maxstat = adf_z_max[regression]
        minstat = adf_z_min[regression]
        starstat = adf_z_star[regression]
        small_p = adf_z_small_p[regression]
        large_p = adf_z_large_p[regression]
    elif dist_type == 'dfgls':
        maxstat = dfgls_tau_max[regression]
        minstat = dfgls_tau_min[regression]
        starstat = dfgls_tau_star[regression]
        small_p = dfgls_small_p[regression]
        large_p = dfgls_large_p[regression]
    else:
        raise ValueError('Unknown test type {0}'.format(dist_type))

    if stat > maxstat:
        return 1.0
    elif stat < minstat:
        return 0.0
    if stat <= starstat:
        poly_coef = small_p
        if dist_type == 'adf-z':
            stat = log(abs(stat))  # Transform stat for small p ADF-z
    else:
        poly_coef = large_p
    return norm.cdf(polyval(poly_coef[::-1], stat))


def mackinnoncrit(num_unit_roots=1, regression='c', nobs=inf,
                  dist_type='ADF-t'):
    """
    Returns the critical values for cointegrating and the ADF test.

    In 2010 MacKinnon updated the values of his 1994 paper with critical values
    for the augmented Dickey-Fuller bootstrap.  These new values are to be
    preferred and are used here.

    Parameters
    ----------
    num_unit_roots : int
        The number of series of I(1) series for which the null of
        non-cointegration is being tested.  For N > 12, the critical values
        are linearly interpolated (not yet implemented).  For the ADF test,
        N = 1.
    regression : {'c', 'tc', 'ctt', 'nc'}, optional
        Following MacKinnon (1996), these stand for the type of regression run.
        'c' for constant and no trend, 'tc' for constant with a linear trend,
        'ctt' for constant with a linear and quadratic trend, and 'nc' for
        no constant.  The values for the no constant case are taken from the
        1996 paper, as they were not updated for 2010 due to the unrealistic
        assumptions that would underlie such a case.
    nobs : {int, np.inf}, optional
        This is the sample size.  If the sample size is numpy.inf, then the
        asymptotic critical values are returned.
    dist_type : {'adf-t', 'adf-z', 'dfgls'}, optional
        Type of test statistic

    Returns
    -------
    crit_vals : ndarray
        Three critical values corresponding to 1%, 5% and 10% cut-offs.

    Notes
    -----
    Results for ADF t-stats from MacKinnon (1994,2010).  Results for DFGLS and
    ADF z-bootstrap use the same methodology as MacKinnon.

    References
    ----------
    MacKinnon, J.G. 1994  "Approximate Asymptotic Distribution Functions for
        Unit-Root and Cointegration Tests." Journal of Business & Economics
        Statistics, 12.2, 167-76.
    MacKinnon, J.G. 2010.  "Critical Values for Cointegration Tests."
        Queen's University, Dept of Economics Working Papers 1227.
        http://ideas.repec.org/p/qed/wpaper/1227.html
    """
    dist_type = dist_type.lower()
    valid_regression = ['c', 'ct', 'nc', 'ctt']
    if dist_type == 'dfgls':
        valid_regression = ['c', 'ct']
    if regression not in valid_regression:
        raise ValueError(
            "regression keyword {0} not understood".format(regression))

    if dist_type == 'adf-t':
        asymptotic_cv = tau_2010[regression][num_unit_roots - 1, :, 0]
        poly_coef = tau_2010[regression][num_unit_roots - 1, :, :].T
    elif dist_type == 'adf-z':
        poly_coef = adf_z_cv_approx[regression].T
        asymptotic_cv = adf_z_cv_approx[regression][:, 0]
    elif dist_type == 'dfgls':
        poly_coef = dfgls_cv_approx[regression].T
        asymptotic_cv = dfgls_cv_approx[regression][:, 0]
    else:
        raise ValueError('Unknown test type {0}'.format(dist_type))

    if nobs is inf:
        return asymptotic_cv
    else:
        # Flip so that highest power to lowest power
        return polyval(poly_coef[::-1], 1. / nobs)


def kpss_crit(stat, trend='c'):
    """
    Linear interpolation for KPSS p-values and critical values

    Parameters
    ----------
    stat : float
        The KPSS test statistic.
    trend : {'c','ct'}
        The trend used when computing the KPSS statistic

    Returns
    -------
    pvalue : float
        The interpolated p-value
    crit_val : ndarray
        Three element array containing the 10%, 5% and 1% critical values,
        in order

    Notes
    -----
    The p-values are linear interpolated from the quantiles of the simulated
    KPSS test statistic distribution using 100,000,000 replications and 2000
    data points.
    """
    table = kpss_critical_values[trend]
    y = table[:, 0]
    x = table[:, 1]
    # kpss.py contains quantiles multiplied by 100
    pvalue = interp(stat, x, y) / 100.0
    cv = [1.0, 5.0, 10.0]
    crit_value = interp(cv, y[::-1], x[::-1])

    return pvalue, crit_value
