import numpy as np

from ray.rllib.models.tf.tf_modelv2 import TFModelV2
from ray.rllib.utils import try_import_tf

tf = try_import_tf()


class DistributionalQTFModel(TFModelV2):
    """Extension of standard TFModel to provide distributional Q values.

    It also supports options for noisy nets and parameter space noise.

    Data flow:
        obs -> forward() -> model_out
        model_out -> get_q_value_distributions() -> Q(s, a) atoms
        model_out -> get_state_value() -> V(s)

    Note that this class by itself is not a valid model unless you
    implement forward() in a subclass."""

    def __init__(
        self,
        obs_space,
        action_space,
        num_outputs,
        model_config,
        name,
        q_hiddens=(256,),
        dueling=False,
        num_atoms=1,
        use_noisy=False,
        v_min=-10.0,
        v_max=10.0,
        sigma0=0.5,
        # TODO(sven): Move `add_layer_norm` into ModelCatalog as
        #  generic option, then error if we use ParameterNoise as
        #  Exploration type and do not have any LayerNorm layers in
        #  the net.
        add_layer_norm=False,
    ):
        """Initialize variables of this model.

        Extra model kwargs:
            q_hiddens (List[int]): List of layer-sizes after(!) the
                Advantages(A)/Value(V)-split. Hence, each of the A- and V-
                branches will have this structure of Dense layers. To define
                the NN before this A/V-split, use - as always -
                config["model"]["fcnet_hiddens"].
            dueling (bool): Whether to build the advantage(A)/value(V) heads
                for DDQN. If True, Q-values are calculated as:
                Q = (A - mean[A]) + V. If False, raw NN output is interpreted
                as Q-values.
            num_atoms (int): if >1, enables distributional DQN
            use_noisy (bool): use noisy nets
            v_min (float): min value support for distributional DQN
            v_max (float): max value support for distributional DQN
            sigma0 (float): initial value of noisy nets
            add_layer_norm (bool): Add a LayerNorm after each layer..

        Note that the core layers for forward() are not defined here, this
        only defines the layers for the Q head. Those layers for forward()
        should be defined in subclasses of DistributionalQModel.
        """

        super(DistributionalQTFModel, self).__init__(
            obs_space, action_space, num_outputs, model_config, name
        )

        # setup the Q head output (i.e., model for get_q_values)
        self.model_out = tf.keras.layers.Input(shape=(num_outputs,), name="model_out")

        def build_action_value(model_out):
            if q_hiddens:
                action_out = model_out
                for i in range(len(q_hiddens)):
                    if use_noisy:
                        action_out = self._noisy_layer(
                            "hidden_%d" % i, action_out, q_hiddens[i], sigma0
                        )
                    elif add_layer_norm:
                        action_out = tf.keras.layers.Dense(
                            units=q_hiddens[i], activation=tf.nn.relu
                        )(action_out)
                        action_out = tf.keras.layers.LayerNormalization()(action_out)
                    else:
                        action_out = tf.keras.layers.Dense(
                            units=q_hiddens[i],
                            activation=tf.nn.relu,
                            name="hidden_%d" % i,
                        )(action_out)
            else:
                # Avoid postprocessing the outputs. This enables custom models
                # to be used for parametric action DQN.
                action_out = model_out

            if use_noisy:
                action_scores = self._noisy_layer(
                    "output",
                    action_out,
                    self.action_space.n * num_atoms,
                    sigma0,
                    non_linear=False,
                )
            elif q_hiddens:
                action_scores = tf.keras.layers.Dense(
                    units=self.action_space.n * num_atoms, activation=None
                )(action_out)
            else:
                action_scores = model_out

            if num_atoms > 1:
                # Distributional Q-learning uses a discrete support z
                # to represent the action value distribution
                z = tf.range(num_atoms, dtype=tf.float32)
                z = v_min + z * (v_max - v_min) / float(num_atoms - 1)

                def _layer(x):
                    support_logits_per_action = tf.reshape(
                        tensor=x, shape=(-1, self.action_space.n, num_atoms)
                    )
                    support_prob_per_action = tf.nn.softmax(
                        logits=support_logits_per_action
                    )
                    x = tf.reduce_sum(input_tensor=z * support_prob_per_action, axis=-1)
                    logits = support_logits_per_action
                    dist = support_prob_per_action
                    return [x, z, support_logits_per_action, logits, dist]

                return tf.keras.layers.Lambda(_layer)(action_scores)
            else:
                logits = tf.expand_dims(tf.ones_like(action_scores), -1)
                dist = tf.expand_dims(tf.ones_like(action_scores), -1)
                return [action_scores, logits, dist]

        def build_state_score(model_out):
            state_out = model_out
            for i in range(len(q_hiddens)):
                if use_noisy:
                    state_out = self._noisy_layer(
                        "dueling_hidden_%d" % i, state_out, q_hiddens[i], sigma0
                    )
                else:
                    state_out = tf.keras.layers.Dense(
                        units=q_hiddens[i], activation=tf.nn.relu
                    )(state_out)
                    if add_layer_norm:
                        state_out = tf.keras.layers.LayerNormalization()(state_out)
            if use_noisy:
                state_score = self._noisy_layer(
                    "dueling_output", state_out, num_atoms, sigma0, non_linear=False
                )
            else:
                state_score = tf.keras.layers.Dense(units=num_atoms, activation=None)(
                    state_out
                )
            return state_score

        if tf.executing_eagerly():
            from tensorflow.python.ops import variable_scope

            # Have to use a variable store to reuse variables in eager mode
            store = variable_scope.EagerVariableStore()

            # Save the scope objects, since in eager we will execute this
            # path repeatedly and there is no guarantee it will always be run
            # in the same original scope.
            with tf.variable_scope(name + "/action_value") as action_scope:
                pass
            with tf.variable_scope(name + "/state_value") as state_scope:
                pass

            def build_action_value_in_scope(model_out):
                with store.as_default():
                    with tf.variable_scope(action_scope, reuse=tf.AUTO_REUSE):
                        return build_action_value(model_out)

            def build_state_score_in_scope(model_out):
                with store.as_default():
                    with tf.variable_scope(state_scope, reuse=tf.AUTO_REUSE):
                        return build_state_score(model_out)

        else:

            def build_action_value_in_scope(model_out):
                with tf.variable_scope(name + "/action_value", reuse=tf.AUTO_REUSE):
                    return build_action_value(model_out)

            def build_state_score_in_scope(model_out):
                with tf.variable_scope(name + "/state_value", reuse=tf.AUTO_REUSE):
                    return build_state_score(model_out)

        q_out = build_action_value_in_scope(self.model_out)
        self.q_value_head = tf.keras.Model(self.model_out, q_out)
        self.register_variables(self.q_value_head.variables)

        if dueling:
            state_out = build_state_score_in_scope(self.model_out)
            self.state_value_head = tf.keras.Model(self.model_out, state_out)
            self.register_variables(self.state_value_head.variables)

    def get_q_value_distributions(self, model_out):
        """Returns distributional values for Q(s, a) given a state embedding.

        Override this in your custom model to customize the Q output head.

        Arguments:
            model_out (Tensor): embedding from the model layers

        Returns:
            (action_scores, logits, dist) if num_atoms == 1, otherwise
            (action_scores, z, support_logits_per_action, logits, dist)
        """

        return self.q_value_head(model_out)

    def get_state_value(self, model_out):
        """Returns the state value prediction for the given state embedding."""

        return self.state_value_head(model_out)

    def _noisy_layer(self, prefix, action_in, out_size, sigma0, non_linear=True):
        """
        a common dense layer: y = w^{T}x + b
        a noisy layer: y = (w + \\epsilon_w*\\sigma_w)^{T}x +
            (b+\\epsilon_b*\\sigma_b)
        where \epsilon are random variables sampled from factorized normal
        distributions and \\sigma are trainable variables which are expected to
        vanish along the training procedure
        """
        in_size = int(action_in.shape[1])

        epsilon_in = tf.random_normal(shape=[in_size])
        epsilon_out = tf.random_normal(shape=[out_size])
        epsilon_in = self._f_epsilon(epsilon_in)
        epsilon_out = self._f_epsilon(epsilon_out)
        epsilon_w = tf.matmul(
            a=tf.expand_dims(epsilon_in, -1), b=tf.expand_dims(epsilon_out, 0)
        )
        epsilon_b = epsilon_out
        sigma_w = tf.get_variable(
            name=prefix + "_sigma_w",
            shape=[in_size, out_size],
            dtype=tf.float32,
            initializer=tf.random_uniform_initializer(
                minval=-1.0 / np.sqrt(float(in_size)),
                maxval=1.0 / np.sqrt(float(in_size)),
            ),
        )
        # TF noise generation can be unreliable on GPU
        # If generating the noise on the CPU,
        # lowering sigma0 to 0.1 may be helpful
        sigma_b = tf.get_variable(
            name=prefix + "_sigma_b",
            shape=[out_size],
            dtype=tf.float32,  # 0.5~GPU, 0.1~CPU
            initializer=tf.constant_initializer(sigma0 / np.sqrt(float(in_size))),
        )

        w = tf.get_variable(
            name=prefix + "_fc_w",
            shape=[in_size, out_size],
            dtype=tf.float32,
            initializer=tf.initializers.glorot_uniform(),
        )
        b = tf.get_variable(
            name=prefix + "_fc_b",
            shape=[out_size],
            dtype=tf.float32,
            initializer=tf.zeros_initializer(),
        )

        action_activation = tf.keras.layers.Lambda(
            lambda x: tf.matmul(x, w + sigma_w * epsilon_w) + b + sigma_b * epsilon_b
        )(action_in)

        if not non_linear:
            return action_activation
        return tf.nn.relu(action_activation)

    def _f_epsilon(self, x):
        return tf.sign(x) * tf.sqrt(tf.abs(x))
