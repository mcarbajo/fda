from functools import singledispatch

from numpy import polyder, polyint, polymul, polyval
import scipy.integrate
from scipy.interpolate import PPoly

import numpy as np

from ...representation.basis import Constant, Monomial, Fourier, BSpline
from .._lfd import LinearDifferentialOperator
from ._regularization import Regularization


@singledispatch
def penalty_matrix_optimized(basis, regularization):
    """
    Return a penalty matrix given a basis.

    This method is a singledispatch method that provides an
    efficient analytical implementation of the computation of the
    penalty matrix if possible.
    """
    return NotImplemented


class LinearDifferentialOperatorRegularization(Regularization):
    """
    Regularization using the integral of the square of a linear differential
    operator.

    Args:
        lfd (LinearDifferentialOperator, list or int): Linear
        differential operator. If it is not a LinearDifferentialOperator
        object, it will be converted to one.

    """

    def __init__(self, linear_diff_op=2):
        self.linear_diff_op = linear_diff_op if (
            isinstance(linear_diff_op, LinearDifferentialOperator)) else (
                LinearDifferentialOperator(linear_diff_op))

    penalty_matrix_optimized = penalty_matrix_optimized

    def penalty_matrix_numerical(self, basis):
        """Return a penalty matrix using a numerical approach.

        Args:
            basis (Basis): basis to compute the penalty for.

        """
        indices = np.triu_indices(basis.n_basis)

        def cross_product(x):
            """Multiply the two lfds"""
            res = self.linear_diff_op(basis)([x])[:, 0]

            return res[indices[0]] * res[indices[1]]

        # Range of first dimension
        domain_range = basis.domain_range[0]

        penalty_matrix = np.empty((basis.n_basis, basis.n_basis))

        # Obtain the integrals for the upper matrix
        triang_vec = scipy.integrate.quad_vec(
            cross_product, domain_range[0], domain_range[1])[0]

        # Set upper matrix
        penalty_matrix[indices] = triang_vec

        # Set lower matrix
        penalty_matrix[(indices[1], indices[0])] = triang_vec

        return penalty_matrix

    def penalty_matrix(self, basis):
        r"""Return a penalty matrix given a basis.

        The penalty matrix is defined as [RS05-5-6-2]_:

        .. math::
            R_{ij} = \int L\phi_i(s) L\phi_j(s) ds

        where :math:`\phi_i(s)` for :math:`i=1, 2, ..., n` are the basis
        functions and :math:`L` is a differential operator.

        Args:
            basis (Basis): basis to compute the penalty for.

        Returns:
            numpy.array: Penalty matrix.

        References:
            .. [RS05-5-6-2] Ramsay, J., Silverman, B. W. (2005). Specifying the
               roughness penalty. In *Functional Data Analysis* (pp. 106-107).
               Springer.

        """
        matrix = penalty_matrix_optimized(basis, self)

        if matrix is NotImplemented:
            return self.penalty_matrix_numerical(basis)
        else:
            return matrix


@LinearDifferentialOperatorRegularization.penalty_matrix_optimized.register
def constant_penalty_matrix_optimized(
        basis: Constant,
        regularization: LinearDifferentialOperatorRegularization):

    coefs = regularization.linear_diff_op.constant_weights()
    if coefs is None:
        return NotImplemented

    return np.array([[coefs[0] ** 2 *
                      (basis.domain_range[0][1] -
                       basis.domain_range[0][0])]])


def _monomial_evaluate_constant_linear_diff_op(basis, weights):
    """
    Evaluate constant weights of a linear differential operator
    over the basis functions.
    """

    max_derivative = len(weights) - 1

    seq = np.arange(basis.n_basis)
    coef_mat = np.linspace(seq, seq - max_derivative + 1,
                           max_derivative, dtype=int)

    # Compute coefficients for each derivative
    coefs = np.cumprod(coef_mat, axis=0)

    # Add derivative 0 row
    coefs = np.concatenate((np.ones((1, basis.n_basis)), coefs))

    # Now each row correspond to each basis and each column to
    # each derivative
    coefs_t = coefs.T

    # Multiply by the weights
    weighted_coefs = coefs_t * weights
    assert len(weighted_coefs) == basis.n_basis

    # Now each row has the right weight, but the polynomials are in a
    # decreasing order and with different exponents

    # Resize the coefs so that there are as many rows as the number of
    # basis
    # The matrix is now triangular
    # refcheck is False to prevent exceptions while debugging
    weighted_coefs = np.copy(weighted_coefs.T)
    weighted_coefs.resize(basis.n_basis,
                          basis.n_basis, refcheck=False)
    weighted_coefs = weighted_coefs.T

    # Shift the coefficients so that they correspond to the right
    # exponent
    indexes = np.tril_indices(basis.n_basis)
    polynomials = np.zeros_like(weighted_coefs)
    polynomials[indexes[0], indexes[1] -
                indexes[0] - 1] = weighted_coefs[indexes]

    # At this point, each row of the matrix correspond to a polynomial
    # that is the result of applying the linear differential operator
    # to each element of the basis

    return polynomials


@LinearDifferentialOperatorRegularization.penalty_matrix_optimized.register
def monomial_penalty_matrix_optimized(
        basis: Monomial,
        regularization: LinearDifferentialOperatorRegularization):

    weights = regularization.linear_diff_op.constant_weights()
    if weights is None:
        return NotImplemented

    polynomials = _monomial_evaluate_constant_linear_diff_op(basis, weights)

    # Expand the polinomials with 0, so that the multiplication fits
    # inside. It will need the double of the degree
    length_with_padding = polynomials.shape[1] * 2 - 1

    # Multiplication of polynomials is a convolution.
    # The convolution can be performed in parallel applying a Fourier
    # transform and then doing a normal multiplication in that
    # space, coverting back with the inverse Fourier transform
    fft = np.fft.rfft(polynomials, length_with_padding)

    # We compute only the upper matrix, as the penalty matrix is
    # symmetrical
    indices = np.triu_indices(basis.n_basis)
    fft_mul = fft[indices[0]] * fft[indices[1]]

    integrand = np.fft.irfft(fft_mul, length_with_padding)

    integration_domain = basis.domain_range[0]

    # To integrate, divide by the position and increase the exponent
    # in the evaluation
    denom = np.arange(integrand.shape[1], 0, -1)
    integrand /= denom

    # Add column of zeros at the right to increase exponent
    integrand = np.pad(integrand,
                       pad_width=((0, 0),
                                  (0, 1)),
                       mode='constant')

    # Now, apply Barrow's rule
    # polyval applies Horner method over the first dimension,
    # so we need to transpose
    x_right = np.polyval(integrand.T, integration_domain[1])
    x_left = np.polyval(integrand.T, integration_domain[0])

    integral = x_right - x_left

    penalty_matrix = np.empty((basis.n_basis, basis.n_basis))

    # Set upper matrix
    penalty_matrix[indices] = integral

    # Set lower matrix
    penalty_matrix[(indices[1], indices[0])] = integral

    return penalty_matrix


def _fourier_penalty_matrix_optimized_orthonormal(basis, weights):
    """
    Return the penalty when the basis is orthonormal.
    """

    signs = np.array([1, 1, -1, -1])
    signs_expanded = np.tile(signs, len(weights) // 4 + 1)

    signs_odd = signs_expanded[:len(weights)]
    signs_even = signs_expanded[1:len(weights) + 1]

    phases = (np.arange(1, (basis.n_basis - 1) // 2 + 1) *
              2 * np.pi / basis.period)

    # Compute increasing powers
    coefs_no_sign = np.vander(phases, len(weights), increasing=True)

    coefs_no_sign *= weights

    coefs_odd = signs_odd * coefs_no_sign
    coefs_even = signs_even * coefs_no_sign

    # After applying the linear differential operator to a sinusoidal
    # element of the basis e, the result can be expressed as
    # A e + B e*, where e* is the other basis element in the pair
    # with the same phase

    odd_sin_coefs = np.sum(coefs_odd[:, ::2], axis=1)
    odd_cos_coefs = np.sum(coefs_odd[:, 1::2], axis=1)

    even_cos_coefs = np.sum(coefs_even[:, ::2], axis=1)
    even_sin_coefs = np.sum(coefs_even[:, 1::2], axis=1)

    # The diagonal is the inner product of A e + B e*
    # with itself. As the basis is orthonormal, the cross products e e*
    # are 0, and the products e e and e* e* are one.
    # Thus, the diagonal is A^2 + B^2
    # All elements outside the main diagonal are 0
    main_diag_odd = odd_sin_coefs**2 + odd_cos_coefs**2
    main_diag_even = even_sin_coefs**2 + even_cos_coefs**2

    # The main diagonal should intercalate both diagonals
    main_diag = np.array((main_diag_odd, main_diag_even)).T.ravel()

    penalty_matrix = np.diag(main_diag)

    # Add row and column for the constant
    penalty_matrix = np.pad(penalty_matrix, pad_width=((1, 0), (1, 0)),
                            mode='constant')

    penalty_matrix[0, 0] = weights[0]**2

    return penalty_matrix


@LinearDifferentialOperatorRegularization.penalty_matrix_optimized.register
def fourier_penalty_matrix_optimized(
        basis: Fourier,
        regularization: LinearDifferentialOperatorRegularization):

    weights = regularization.linear_diff_op.constant_weights()
    if weights is None:
        return NotImplemented

    # If the period and domain range are not the same, the basis functions
    # are not orthogonal
    if basis.period != (basis.domain_range[0][1] - basis.domain_range[0][0]):
        return NotImplemented

    return _fourier_penalty_matrix_optimized_orthonormal(basis, weights)


@LinearDifferentialOperatorRegularization.penalty_matrix_optimized.register
def bspline_penalty_matrix_optimized(
        basis: BSpline,
        regularization: LinearDifferentialOperatorRegularization):

    coefs = regularization.linear_diff_op.constant_weights()
    if coefs is None:
        return NotImplemented

    nonzero = np.flatnonzero(coefs)

    # All derivatives above the order of the spline are effectively
    # zero
    nonzero = nonzero[nonzero < basis.order]

    if len(nonzero) == 0:
        return np.zeros((basis.n_basis, basis.n_basis))

    # We will only deal with one nonzero coefficient right now
    if len(nonzero) != 1:
        return NotImplemented

    derivative_degree = nonzero[0]

    if derivative_degree == basis.order - 1:
        # The derivative of the bsplines are constant in the intervals
        # defined between knots
        knots = np.array(basis.knots)
        mid_inter = (knots[1:] + knots[:-1]) / 2
        constants = basis.evaluate(mid_inter,
                                   derivative=derivative_degree).T
        knots_intervals = np.diff(basis.knots)
        # Integration of product of constants
        return constants.T @ np.diag(knots_intervals) @ constants

    # We only deal with the case without zero length intervals
    # for now
    if np.any(np.diff(basis.knots) == 0):
        return NotImplemented

    # Compute exactly using the piecewise polynomial
    # representation of splines

    # Places m knots at the boundaries
    knots = basis._evaluation_knots()

    # c is used the select which spline the function
    # PPoly.from_spline below computes
    c = np.zeros(len(knots))

    # Initialise empty list to store the piecewise polynomials
    ppoly_lst = []

    no_0_intervals = np.where(np.diff(knots) > 0)[0]

    # For each basis gets its piecewise polynomial representation
    for i in range(basis.n_basis):

        # Write a 1 in c in the position of the spline
        # transformed in each iteration
        c[i] = 1

        # Gets the piecewise polynomial representation and gets
        # only the positions for no zero length intervals
        # This polynomial are defined relatively to the knots
        # meaning that the column i corresponds to the ith knot.
        # Let the ith knot be a
        # Then f(x) = pp(x - a)
        pp = PPoly.from_spline((knots, c, basis.order - 1))
        pp_coefs = pp.c[:, no_0_intervals]

        # We have the coefficients for each interval in coordinates
        # (x - a), so we will need to subtract a when computing the
        # definite integral
        ppoly_lst.append(pp_coefs)
        c[i] = 0

    # Now for each pair of basis computes the inner product after
    # applying the linear differential operator
    penalty_matrix = np.zeros((basis.n_basis, basis.n_basis))
    for interval in range(len(no_0_intervals)):
        for i in range(basis.n_basis):
            poly_i = np.trim_zeros(ppoly_lst[i][:,
                                                interval], 'f')
            if len(poly_i) <= derivative_degree:
                # if the order of the polynomial is lesser or
                # equal to the derivative the result of the
                # integral will be 0
                continue
            # indefinite integral
            derivative = polyder(poly_i, derivative_degree)
            square = polymul(derivative, derivative)
            integral = polyint(square)

            # definite integral
            penalty_matrix[i, i] += np.diff(polyval(
                integral, basis.knots[interval: interval + 2]
                - basis.knots[interval]))[0]

            for j in range(i + 1, basis.n_basis):
                poly_j = np.trim_zeros(ppoly_lst[j][:,
                                                    interval], 'f')
                if len(poly_j) <= derivative_degree:
                    # if the order of the polynomial is lesser
                    # or equal to the derivative the result of
                    # the integral will be 0
                    continue
                    # indefinite integral
                integral = polyint(
                    polymul(polyder(poly_i, derivative_degree),
                            polyder(poly_j, derivative_degree)))
                # definite integral
                penalty_matrix[i, j] += np.diff(polyval(
                    integral, basis.knots[interval: interval + 2]
                    - basis.knots[interval])
                )[0]
                penalty_matrix[j, i] = penalty_matrix[i, j]
    return penalty_matrix
