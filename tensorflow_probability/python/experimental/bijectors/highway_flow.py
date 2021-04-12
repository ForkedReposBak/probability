import tensorflow.compat.v2 as tf

from tensorflow_probability.python import bijectors as tfb
from tensorflow_probability.python import util
from tensorflow_probability.python.internal import cache_util
from tensorflow_probability.python.internal import samplers


def build_highway_flow_layer(width, residual_fraction_initial_value=0.5, activation_fn=False, seed=None):
    # TODO: add control that residual_fraction_initial_value is between 0 and 1
    residual_fraction_initial_value = tf.convert_to_tensor(residual_fraction_initial_value,
                                                           dtype_hint=tf.float32,
                                                           name='residual_fraction_initial_value')
    dtype = residual_fraction_initial_value.dtype

    bias_seed, upper_seed, lower_seed, diagonal_seed = samplers.split_seed(seed, n=4)

    return HighwayFlow(
        width=width,
        residual_fraction=util.TransformedVariable(
            initial_value=residual_fraction_initial_value,
            bijector=tfb.Sigmoid(),
            dtype=dtype),
        activation_fn=activation_fn,
        bias=tf.Variable(samplers.normal((width,), mean=0., stddev=0.01, seed=bias_seed), dtype=dtype),
        upper_diagonal_weights_matrix=util.TransformedVariable(
            initial_value=tf.experimental.numpy.tril(
                samplers.normal((width, width), mean=0., stddev=1., seed=upper_seed),
                k=-1) + tf.linalg.diag(
                samplers.uniform((width,), minval=0., maxval=1., seed=diagonal_seed)),
            bijector=tfb.FillScaleTriL(diag_bijector=tfb.Softplus(), diag_shift=None),
            dtype=dtype),
        lower_diagonal_weights_matrix=util.TransformedVariable(
            initial_value=samplers.normal((width, width), mean=0., stddev=1., seed=lower_seed),
            bijector=tfb.Chain([tfb.TransformDiagonal(diag_bijector=tfb.Shift(1.)),
                                tfb.Pad(paddings=[(1, 0), (0, 1)]),
                                tfb.FillTriangular()]),
            dtype=dtype)
    )


class HighwayFlow(tfb.Bijector):
    """Implements an Highway Flow bijector [1], which interpolates the input X with the transformations at each step
    of the bjiector.
    The Highway Flow can be used as building block for a Cascading flow [1] or as a generic normalizig flow.

    The transoformation consists in a convex update between the input X and a linear transformation of X
    with the form g(AX+b), where g is a differentiable non-decreasing activation function, and A and b are trainable.

    The convex update is regulated by a trainable residual fraction 'l' constrained between 0 and 1, and can be
    formalized as:
    Y = l*X + (1-l) * g(AX + b).

    To make this transformation invertible, the bijector is split in three convex updates:
     - Y1 = l*X + (1-l) * LX, with L lower diagonal matrix with ones on the diagonal;
     - Y2 = l*Y1 + (1-l) * UY1 + b, with U upper diagonal matrix with positive diagonal;
     - Y = l*Y2 + (1-l) * g(Y2)

    The function build_highway_flow_layer helps initializing the bijector with the variables respecting the various
    constraints.

    For more details on Highway Flow and Cascading Flows see [1].

    #### Usage example:
    ```python
    tfd = tfp.distributions
    tfb = tfp.bijectors

    dim = 4 # last input dimension

    bijector = build_highway_flow_layer(dim, activation_fn=True)
    y = bijector.forward(x)  # forward mapping
    x = bijector.inverse(y)  # inverse mapping
    base = tfd.MultivariateNormalDiag(loc=tf.zeros(dim)) # Base distribution
    transformed_distribution = tfd.TransformedDistribution(base, bijector)
    ```

    #### References

    [1]: Ambrogioni, Luca, Gianluigi Silvestri, and Marcel van Gerven. "Automatic variational inference with
    cascading flows." arXiv preprint arXiv:2102.04801 (2021).
    """

    # HighWay Flow simultaneously computes `forward` and `fldj` (and `inverse`/`ildj`),
    # so we override the bijector cache to update the LDJ entries of attrs on
    # forward/inverse inverse calls (instead of updating them only when the LDJ
    # methods themselves are called).
    _cache = cache_util.BijectorCacheWithGreedyAttrs(
        forward_name='_augmented_forward',
        inverse_name='_augmented_inverse')

    def __init__(self, width, residual_fraction, activation_fn, bias, upper_diagonal_weights_matrix,
                 lower_diagonal_weights_matrix, validate_args=False,
                 name='highway_flow'):
        '''
        Args:
            width: needs to be equal to the size of the matrices U, L and of the bias vector
            bias: bias parameter
            residual_fraction: variable used for the convex update, must be between 0 and 1
            activation_fn: not used yet
            bias: bias vector
            upper_diagonal_weights_matrix: Lower diagional matrix of size (width, width) with positive diagonal
            (is transposed to Upper diagonal within the bijector)
            lower_diagonal_weights_matrix: Lower diagonal matrix with ones on the main diagional.
        '''
        parameters = dict(locals())
        with tf.name_scope(name) as name:
            self._width = width
            self._bias = bias
            self._residual_fraction = residual_fraction
            # still lower triangular, transpose is done in matvec.
            self._upper_diagonal_weights_matrix = upper_diagonal_weights_matrix
            self._lower_diagonal_weights_matrix = lower_diagonal_weights_matrix
            self._activation_fn = activation_fn

            super(HighwayFlow, self).__init__(
                validate_args=validate_args,
                forward_min_event_ndims=1,
                parameters=parameters,
                name=name)

    @property
    def bias(self):
        return self._bias

    @property
    def width(self):
        return self._width

    @property
    def residual_fraction(self):
        return self._residual_fraction

    @property
    def upper_diagonal_weights_matrix(self):
        return self._upper_diagonal_weights_matrix

    @property
    def lower_diagonal_weights_matrix(self):
        return self._lower_diagonal_weights_matrix

    @property
    def activation_fn(self):
        return self._activation_fn

    def df(self, x):
        # derivative of activation
        return self.residual_fraction + (1. - self.residual_fraction) * tf.math.sigmoid(x)

    def _convex_update(self, weights_matrix):
        # convex update
        # same as in the paper, but probably easier to invert
        return self.residual_fraction * tf.eye(self.width) + (1. - self.residual_fraction) * weights_matrix

    def inv_f(self, y, N=20):
        # inverse with Newton iteration
        x = tf.ones(y.shape)
        for _ in range(N):
            x = x - (self.residual_fraction * x + (1. - self.residual_fraction) * tf.math.softplus(x) - y) / (
                self.df(x))
        return x

    def _augmented_forward(self, x):
        # upper mmatrix jacobian
        fldj = tf.zeros(x.shape[:-1]) + tf.reduce_sum(
            tf.math.log(self.residual_fraction + (1. - self.residual_fraction) * tf.linalg.diag_part(
                self.upper_diagonal_weights_matrix)))  # jacobian from upper matrix
        # jacobian from lower matrix is 0

        # todo: see how to optimize _convex_update
        x = tf.linalg.matvec(self._convex_update(self.lower_diagonal_weights_matrix), x)
        x = tf.linalg.matvec(tf.transpose(self._convex_update(self.upper_diagonal_weights_matrix)),
                             x) + (
                    1 - self.residual_fraction) * self.bias  # in the implementation there was only one bias
        if self.activation_fn:
            fldj += tf.reduce_sum(tf.math.log(self.df(x)), -1)
            x = self.residual_fraction * x + (1. - self.residual_fraction) * self.activation_fn(x)
            '''activation_layer = scalar_function_with_inferred_inverse.ScalarFunctionWithInferredInverse(
                lambda x: self.residual_fraction * x + (1. - self.residual_fraction) * self.activation_fn(x))
            fldj += activation_layer.forward_log_det_jacobian(x, self.forward_min_event_ndims)
            x = activation_layer.forward(x)'''
        return x, {'ildj': -fldj, 'fldj': fldj}

    def _augmented_inverse(self, y):
        ildj = tf.zeros(y.shape[:-1]) - tf.reduce_sum(
            tf.math.log(self.residual_fraction + (1. - self.residual_fraction) * tf.linalg.diag_part(
                self.upper_diagonal_weights_matrix)))
        if self.activation_fn:
            y = self.inv_f(y)
            ildj -= tf.reduce_sum(tf.math.log(self.df(y)), -1)
            # residual_fraction = tf.identity(self.residual_fraction)
            '''activation_layer = scalar_function_with_inferred_inverse.ScalarFunctionWithInferredInverse(
                lambda x: residual_fraction * x + (1. - residual_fraction) * self.activation_fn(x))
            ildj += activation_layer.inverse_log_det_jacobian(y, self.forward_min_event_ndims)
            y = activation_layer.inverse(y)'''

        # this works with y having shape [BATCH x WIDTH], don't know how well it generalizes
        y = tf.linalg.triangular_solve(tf.transpose(self._convex_update(self.upper_diagonal_weights_matrix)),
                                       tf.linalg.matrix_transpose(y - (1 - self.residual_fraction) * self.bias),
                                       lower=False)
        y = tf.linalg.triangular_solve(self._convex_update(self.lower_diagonal_weights_matrix), y)
        return tf.linalg.matrix_transpose(y), {'ildj': ildj, 'fldj': -ildj}

    def _forward(self, x):
        y, _ = self._augmented_forward(x)
        return y

    def _inverse(self, y):
        x, _ = self._augmented_inverse(y)
        return x

    def _forward_log_det_jacobian(self, x):
        cached = self._cache.forward_attributes(x)
        # If LDJ isn't in the cache, call forward once.
        if 'fldj' not in cached:
            _, attrs = self._augmented_forward(x)
            cached.update(attrs)
        return cached['fldj']

    def _inverse_log_det_jacobian(self, y):
        cached = self._cache.inverse_attributes(y)
        # If LDJ isn't in the cache, call inverse once.
        if 'ildj' not in cached:
            _, attrs = self._augmented_inverse(y)
            cached.update(attrs)
        return cached['ildj']
