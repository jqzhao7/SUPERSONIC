from gym.spaces import Discrete
import numpy as np

import ray
from ray.rllib.agents.dqn.distributional_q_tf_model import DistributionalQTFModel
from ray.rllib.agents.dqn.simple_q_tf_policy import TargetNetworkMixin
from ray.rllib.models import ModelCatalog
from ray.rllib.models.tf.tf_action_dist import Categorical
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.tf_policy import LearningRateSchedule
from ray.rllib.policy.tf_policy_template import build_tf_policy
from ray.rllib.utils.error import UnsupportedSpaceException
from ray.rllib.utils.exploration import ParameterNoise
from ray.rllib.utils.framework import try_import_tf
from ray.rllib.utils.tf_ops import huber_loss, reduce_mean_ignore_inf, minimize_and_clip
from ray.rllib.utils.tf_ops import make_tf_callable

tf = try_import_tf()

Q_SCOPE = "q_func"
Q_TARGET_SCOPE = "target_q_func"

# Importance sampling weights for prioritized replay
PRIO_WEIGHTS = "weights"


class QLoss:
    def __init__(
        self,
        q_t_selected,
        q_logits_t_selected,
        q_tp1_best,
        q_dist_tp1_best,
        importance_weights,
        rewards,
        done_mask,
        gamma=0.99,
        n_step=1,
        num_atoms=1,
        v_min=-10.0,
        v_max=10.0,
    ):

        if num_atoms > 1:
            # Distributional Q-learning which corresponds to an entropy loss

            z = tf.range(num_atoms, dtype=tf.float32)
            z = v_min + z * (v_max - v_min) / float(num_atoms - 1)

            # (batch_size, 1) * (1, num_atoms) = (batch_size, num_atoms)
            r_tau = tf.expand_dims(rewards, -1) + gamma ** n_step * tf.expand_dims(
                1.0 - done_mask, -1
            ) * tf.expand_dims(z, 0)
            r_tau = tf.clip_by_value(r_tau, v_min, v_max)
            b = (r_tau - v_min) / ((v_max - v_min) / float(num_atoms - 1))
            lb = tf.floor(b)
            ub = tf.ceil(b)
            # indispensable judgement which is missed in most implementations
            # when b happens to be an integer, lb == ub, so pr_j(s', a*) will
            # be discarded because (ub-b) == (b-lb) == 0
            floor_equal_ceil = tf.to_float(tf.less(ub - lb, 0.5))

            l_project = tf.one_hot(
                tf.cast(lb, dtype=tf.int32), num_atoms
            )  # (batch_size, num_atoms, num_atoms)
            u_project = tf.one_hot(
                tf.cast(ub, dtype=tf.int32), num_atoms
            )  # (batch_size, num_atoms, num_atoms)
            ml_delta = q_dist_tp1_best * (ub - b + floor_equal_ceil)
            mu_delta = q_dist_tp1_best * (b - lb)
            ml_delta = tf.reduce_sum(l_project * tf.expand_dims(ml_delta, -1), axis=1)
            mu_delta = tf.reduce_sum(u_project * tf.expand_dims(mu_delta, -1), axis=1)
            m = ml_delta + mu_delta

            # Rainbow paper claims that using this cross entropy loss for
            # priority is robust and insensitive to `prioritized_replay_alpha`
            self.td_error = tf.nn.softmax_cross_entropy_with_logits(
                labels=m, logits=q_logits_t_selected
            )
            self.loss = tf.reduce_mean(
                self.td_error * tf.cast(importance_weights, tf.float32)
            )
            self.stats = {
                # TODO: better Q stats for dist dqn
                "mean_td_error": tf.reduce_mean(self.td_error),
            }
        else:
            q_tp1_best_masked = (1.0 - done_mask) * q_tp1_best

            # compute RHS of bellman equation
            q_t_selected_target = rewards + gamma ** n_step * q_tp1_best_masked

            # compute the error (potentially clipped)
            self.td_error = q_t_selected - tf.stop_gradient(q_t_selected_target)
            self.loss = tf.reduce_mean(
                tf.cast(importance_weights, tf.float32) * huber_loss(self.td_error)
            )
            self.stats = {
                "mean_q": tf.reduce_mean(q_t_selected),
                "min_q": tf.reduce_min(q_t_selected),
                "max_q": tf.reduce_max(q_t_selected),
                "mean_td_error": tf.reduce_mean(self.td_error),
            }


class ComputeTDErrorMixin:
    def __init__(self):
        @make_tf_callable(self.get_session(), dynamic_shape=True)
        def compute_td_error(
            obs_t, act_t, rew_t, obs_tp1, done_mask, importance_weights
        ):
            # Do forward pass on loss to update td error attribute
            build_q_losses(
                self,
                self.model,
                None,
                {
                    SampleBatch.CUR_OBS: tf.convert_to_tensor(obs_t),
                    SampleBatch.ACTIONS: tf.convert_to_tensor(act_t),
                    SampleBatch.REWARDS: tf.convert_to_tensor(rew_t),
                    SampleBatch.NEXT_OBS: tf.convert_to_tensor(obs_tp1),
                    SampleBatch.DONES: tf.convert_to_tensor(done_mask),
                    PRIO_WEIGHTS: tf.convert_to_tensor(importance_weights),
                },
            )

            return self.q_loss.td_error

        self.compute_td_error = compute_td_error


def build_q_model(policy, obs_space, action_space, config):

    if not isinstance(action_space, Discrete):
        raise UnsupportedSpaceException(
            "Action space {} is not supported for DQN.".format(action_space)
        )

    if config["hiddens"]:
        # try to infer the last layer size, otherwise fall back to 256
        num_outputs = ([256] + config["model"]["fcnet_hiddens"])[-1]
        config["model"]["no_final_linear"] = True
    else:
        num_outputs = action_space.n

    policy.q_model = ModelCatalog.get_model_v2(
        obs_space=obs_space,
        action_space=action_space,
        num_outputs=num_outputs,
        model_config=config["model"],
        framework="tf",
        model_interface=DistributionalQTFModel,
        name=Q_SCOPE,
        num_atoms=config["num_atoms"],
        dueling=config["dueling"],
        q_hiddens=config["hiddens"],
        use_noisy=config["noisy"],
        v_min=config["v_min"],
        v_max=config["v_max"],
        sigma0=config["sigma0"],
        # TODO(sven): Move option to add LayerNorm after each Dense
        #  generically into ModelCatalog.
        add_layer_norm=isinstance(getattr(policy, "exploration", None), ParameterNoise)
        or config["exploration_config"]["type"] == "ParameterNoise",
    )

    policy.target_q_model = ModelCatalog.get_model_v2(
        obs_space=obs_space,
        action_space=action_space,
        num_outputs=num_outputs,
        model_config=config["model"],
        framework="tf",
        model_interface=DistributionalQTFModel,
        name=Q_TARGET_SCOPE,
        num_atoms=config["num_atoms"],
        dueling=config["dueling"],
        q_hiddens=config["hiddens"],
        use_noisy=config["noisy"],
        v_min=config["v_min"],
        v_max=config["v_max"],
        sigma0=config["sigma0"],
        # TODO(sven): Move option to add LayerNorm after each Dense
        #  generically into ModelCatalog.
        add_layer_norm=isinstance(getattr(policy, "exploration", None), ParameterNoise)
        or config["exploration_config"]["type"] == "ParameterNoise",
    )

    return policy.q_model


def get_distribution_inputs_and_class(
    policy, model, obs_batch, *, explore=True, **kwargs
):
    q_vals = compute_q_values(policy, model, obs_batch, explore)
    q_vals = q_vals[0] if isinstance(q_vals, tuple) else q_vals

    policy.q_values = q_vals
    policy.q_func_vars = model.variables()
    return policy.q_values, Categorical, []  # state-out


def build_q_losses(policy, model, _, train_batch):
    config = policy.config
    # q network evaluation
    q_t, q_logits_t, q_dist_t = compute_q_values(
        policy, policy.q_model, train_batch[SampleBatch.CUR_OBS], explore=False
    )

    # target q network evalution
    q_tp1, q_logits_tp1, q_dist_tp1 = compute_q_values(
        policy, policy.target_q_model, train_batch[SampleBatch.NEXT_OBS], explore=False
    )
    policy.target_q_func_vars = policy.target_q_model.variables()

    # q scores for actions which we know were selected in the given state.
    one_hot_selection = tf.one_hot(
        tf.cast(train_batch[SampleBatch.ACTIONS], tf.int32), policy.action_space.n
    )
    q_t_selected = tf.reduce_sum(q_t * one_hot_selection, 1)
    q_logits_t_selected = tf.reduce_sum(
        q_logits_t * tf.expand_dims(one_hot_selection, -1), 1
    )

    # compute estimate of best possible value starting from state at t + 1
    if config["double_q"]:
        (
            q_tp1_using_online_net,
            q_logits_tp1_using_online_net,
            q_dist_tp1_using_online_net,
        ) = compute_q_values(
            policy, policy.q_model, train_batch[SampleBatch.NEXT_OBS], explore=False
        )
        q_tp1_best_using_online_net = tf.argmax(q_tp1_using_online_net, 1)
        q_tp1_best_one_hot_selection = tf.one_hot(
            q_tp1_best_using_online_net, policy.action_space.n
        )
        q_tp1_best = tf.reduce_sum(q_tp1 * q_tp1_best_one_hot_selection, 1)
        q_dist_tp1_best = tf.reduce_sum(
            q_dist_tp1 * tf.expand_dims(q_tp1_best_one_hot_selection, -1), 1
        )
    else:
        q_tp1_best_one_hot_selection = tf.one_hot(
            tf.argmax(q_tp1, 1), policy.action_space.n
        )
        q_tp1_best = tf.reduce_sum(q_tp1 * q_tp1_best_one_hot_selection, 1)
        q_dist_tp1_best = tf.reduce_sum(
            q_dist_tp1 * tf.expand_dims(q_tp1_best_one_hot_selection, -1), 1
        )

    policy.q_loss = QLoss(
        q_t_selected,
        q_logits_t_selected,
        q_tp1_best,
        q_dist_tp1_best,
        train_batch[PRIO_WEIGHTS],
        train_batch[SampleBatch.REWARDS],
        tf.cast(train_batch[SampleBatch.DONES], tf.float32),
        config["gamma"],
        config["n_step"],
        config["num_atoms"],
        config["v_min"],
        config["v_max"],
    )

    return policy.q_loss.loss


def adam_optimizer(policy, config):
    return tf.train.AdamOptimizer(
        learning_rate=policy.cur_lr, epsilon=config["adam_epsilon"]
    )


def clip_gradients(policy, optimizer, loss):
    if policy.config["grad_clip"] is not None:
        grads_and_vars = minimize_and_clip(
            optimizer,
            loss,
            var_list=policy.q_func_vars,
            clip_val=policy.config["grad_clip"],
        )
    else:
        grads_and_vars = optimizer.compute_gradients(loss, var_list=policy.q_func_vars)
    grads_and_vars = [(g, v) for (g, v) in grads_and_vars if g is not None]
    return grads_and_vars


def build_q_stats(policy, batch):
    return dict({"cur_lr": tf.cast(policy.cur_lr, tf.float64),}, **policy.q_loss.stats)


def setup_early_mixins(policy, obs_space, action_space, config):
    LearningRateSchedule.__init__(policy, config["lr"], config["lr_schedule"])


def setup_mid_mixins(policy, obs_space, action_space, config):
    ComputeTDErrorMixin.__init__(policy)


def setup_late_mixins(policy, obs_space, action_space, config):
    TargetNetworkMixin.__init__(policy, obs_space, action_space, config)


def compute_q_values(policy, model, obs, explore):
    config = policy.config

    model_out, state = model(
        {
            SampleBatch.CUR_OBS: obs,
            "is_training": policy._get_is_training_placeholder(),
        },
        [],
        None,
    )

    if config["num_atoms"] > 1:
        (
            action_scores,
            z,
            support_logits_per_action,
            logits,
            dist,
        ) = model.get_q_value_distributions(model_out)
    else:
        (action_scores, logits, dist) = model.get_q_value_distributions(model_out)

    if config["dueling"]:
        state_score = model.get_state_value(model_out)
        if config["num_atoms"] > 1:
            support_logits_per_action_mean = tf.reduce_mean(
                support_logits_per_action, 1
            )
            support_logits_per_action_centered = (
                support_logits_per_action
                - tf.expand_dims(support_logits_per_action_mean, 1)
            )
            support_logits_per_action = (
                tf.expand_dims(state_score, 1) + support_logits_per_action_centered
            )
            support_prob_per_action = tf.nn.softmax(logits=support_logits_per_action)
            value = tf.reduce_sum(input_tensor=z * support_prob_per_action, axis=-1)
            logits = support_logits_per_action
            dist = support_prob_per_action
        else:
            action_scores_mean = reduce_mean_ignore_inf(action_scores, 1)
            action_scores_centered = action_scores - tf.expand_dims(
                action_scores_mean, 1
            )
            value = state_score + action_scores_centered
    else:
        value = action_scores

    return value, logits, dist


def _adjust_nstep(n_step, gamma, obs, actions, rewards, new_obs, dones):
    """Rewrites the given trajectory fragments to encode n-step rewards.

    reward[i] = (
        reward[i] * gamma**0 +
        reward[i+1] * gamma**1 +
        ... +
        reward[i+n_step-1] * gamma**(n_step-1))

    The ith new_obs is also adjusted to point to the (i+n_step-1)'th new obs.

    At the end of the trajectory, n is truncated to fit in the traj length.
    """

    assert not any(dones[:-1]), "Unexpected done in middle of trajectory"

    traj_length = len(rewards)
    for i in range(traj_length):
        for j in range(1, n_step):
            if i + j < traj_length:
                new_obs[i] = new_obs[i + j]
                dones[i] = dones[i + j]
                rewards[i] += gamma ** j * rewards[i + j]


def postprocess_nstep_and_prio(policy, batch, other_agent=None, episode=None):
    # N-step Q adjustments
    if policy.config["n_step"] > 1:
        _adjust_nstep(
            policy.config["n_step"],
            policy.config["gamma"],
            batch[SampleBatch.CUR_OBS],
            batch[SampleBatch.ACTIONS],
            batch[SampleBatch.REWARDS],
            batch[SampleBatch.NEXT_OBS],
            batch[SampleBatch.DONES],
        )

    if PRIO_WEIGHTS not in batch:
        batch[PRIO_WEIGHTS] = np.ones_like(batch[SampleBatch.REWARDS])

    # Prioritize on the worker side
    if batch.count > 0 and policy.config["worker_side_prioritization"]:
        td_errors = policy.compute_td_error(
            batch[SampleBatch.CUR_OBS],
            batch[SampleBatch.ACTIONS],
            batch[SampleBatch.REWARDS],
            batch[SampleBatch.NEXT_OBS],
            batch[SampleBatch.DONES],
            batch[PRIO_WEIGHTS],
        )
        new_priorities = np.abs(td_errors) + policy.config["prioritized_replay_eps"]
        batch.data[PRIO_WEIGHTS] = new_priorities

    return batch


DQNTFPolicy = build_tf_policy(
    name="DQNTFPolicy",
    get_default_config=lambda: ray.rllib.agents.dqn.dqn.DEFAULT_CONFIG,
    make_model=build_q_model,
    action_distribution_fn=get_distribution_inputs_and_class,
    loss_fn=build_q_losses,
    stats_fn=build_q_stats,
    postprocess_fn=postprocess_nstep_and_prio,
    optimizer_fn=adam_optimizer,
    gradients_fn=clip_gradients,
    extra_action_fetches_fn=lambda policy: {"q_values": policy.q_values},
    extra_learn_fetches_fn=lambda policy: {"td_error": policy.q_loss.td_error},
    before_init=setup_early_mixins,
    before_loss_init=setup_mid_mixins,
    after_init=setup_late_mixins,
    obs_include_prev_action_reward=False,
    mixins=[TargetNetworkMixin, ComputeTDErrorMixin, LearningRateSchedule,],
)
