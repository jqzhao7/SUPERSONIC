import numpy as np
from scipy.stats import norm
import unittest

import ray.rllib.agents.dqn as dqn
import ray.rllib.agents.pg as pg
import ray.rllib.agents.ppo as ppo
import ray.rllib.agents.sac as sac
from ray.rllib.utils.framework import try_import_tf
from ray.rllib.utils.test_utils import check, framework_iterator
from ray.rllib.utils.numpy import one_hot, fc, MIN_LOG_NN_OUTPUT, MAX_LOG_NN_OUTPUT

tf = try_import_tf()


def do_test_log_likelihood(
    run,
    config,
    prev_a=None,
    continuous=False,
    layer_key=("fc", (0, 4), ("_hidden_layers.0.", "_logits.")),
    logp_func=None,
):
    config = config.copy()
    # Run locally.
    config["num_workers"] = 0
    # Env setup.
    if continuous:
        env = "Pendulum-v0"
        obs_batch = preprocessed_obs_batch = np.array([[0.0, 0.1, -0.1]])
    else:
        env = "FrozenLake-v0"
        config["env_config"] = {"is_slippery": False, "map_name": "4x4"}
        obs_batch = np.array([0])
        preprocessed_obs_batch = one_hot(obs_batch, depth=16)

    prev_r = None if prev_a is None else np.array(0.0)

    # Test against all frameworks.
    for fw in framework_iterator(config):
        if run in [sac.SACTrainer] and fw == "tfe":
            continue

        trainer = run(config=config, env=env)

        policy = trainer.get_policy()
        vars = policy.get_weights()
        # Sample n actions, then roughly check their logp against their
        # counts.
        num_actions = 1000 if not continuous else 50
        actions = []
        for _ in range(num_actions):
            # Single action from single obs.
            actions.append(
                trainer.compute_action(
                    obs_batch[0], prev_action=prev_a, prev_reward=prev_r, explore=True
                )
            )

        # Test all taken actions for their log-likelihoods vs expected values.
        if continuous:
            for idx in range(num_actions):
                a = actions[idx]
                if fw != "torch":
                    if isinstance(vars, list):
                        expected_mean_logstd = fc(
                            fc(obs_batch, vars[layer_key[1][0]]), vars[layer_key[1][1]]
                        )
                    else:
                        expected_mean_logstd = fc(
                            fc(
                                obs_batch,
                                vars["default_policy/{}_1/kernel".format(layer_key[0])],
                            ),
                            vars["default_policy/{}_out/kernel".format(layer_key[0])],
                        )
                else:
                    expected_mean_logstd = fc(
                        fc(
                            obs_batch,
                            vars["{}_model.0.weight".format(layer_key[2][0])],
                            framework=fw,
                        ),
                        vars["{}_model.0.weight".format(layer_key[2][1])],
                        framework=fw,
                    )
                mean, log_std = np.split(expected_mean_logstd, 2, axis=-1)
                if logp_func is None:
                    expected_logp = np.log(norm.pdf(a, mean, np.exp(log_std)))
                else:
                    expected_logp = logp_func(mean, log_std, a)
                logp = policy.compute_log_likelihoods(
                    np.array([a]),
                    preprocessed_obs_batch,
                    prev_action_batch=np.array([prev_a]),
                    prev_reward_batch=np.array([prev_r]),
                )
                check(logp, expected_logp[0], rtol=0.2)
        # Test all available actions for their logp values.
        else:
            for a in [0, 1, 2, 3]:
                count = actions.count(a)
                expected_prob = count / num_actions
                logp = policy.compute_log_likelihoods(
                    np.array([a]),
                    preprocessed_obs_batch,
                    prev_action_batch=np.array([prev_a]),
                    prev_reward_batch=np.array([prev_r]),
                )
                check(np.exp(logp), expected_prob, atol=0.2)


class TestComputeLogLikelihood(unittest.TestCase):
    def test_dqn(self):
        """Tests, whether DQN correctly computes logp in soft-q mode."""
        config = dqn.DEFAULT_CONFIG.copy()
        # Soft-Q for DQN.
        config["exploration_config"] = {"type": "SoftQ", "temperature": 0.5}
        do_test_log_likelihood(dqn.DQNTrainer, config)

    def test_pg_cont(self):
        """Tests PG's (cont. actions) compute_log_likelihoods method."""
        config = pg.DEFAULT_CONFIG.copy()
        config["model"]["fcnet_hiddens"] = [10]
        config["model"]["fcnet_activation"] = "linear"
        prev_a = np.array([0.0])
        do_test_log_likelihood(
            pg.PGTrainer,
            config,
            prev_a,
            continuous=True,
            layer_key=("fc", (0, 2), ("_hidden_layers.0.", "_logits.")),
        )

    def test_pg_discr(self):
        """Tests PG's (cont. actions) compute_log_likelihoods method."""
        config = pg.DEFAULT_CONFIG.copy()
        prev_a = np.array(0)
        do_test_log_likelihood(pg.PGTrainer, config, prev_a)

    def test_ppo_cont(self):
        """Tests PPO's (cont. actions) compute_log_likelihoods method."""
        config = ppo.DEFAULT_CONFIG.copy()
        config["model"]["fcnet_hiddens"] = [10]
        config["model"]["fcnet_activation"] = "linear"
        prev_a = np.array([0.0])
        do_test_log_likelihood(ppo.PPOTrainer, config, prev_a, continuous=True)

    def test_ppo_discr(self):
        """Tests PPO's (discr. actions) compute_log_likelihoods method."""
        prev_a = np.array(0)
        do_test_log_likelihood(ppo.PPOTrainer, ppo.DEFAULT_CONFIG, prev_a)

    def test_sac_cont(self):
        """Tests SAC's (cont. actions) compute_log_likelihoods method."""
        config = sac.DEFAULT_CONFIG.copy()
        config["policy_model"]["fcnet_hiddens"] = [10]
        config["policy_model"]["fcnet_activation"] = "linear"
        prev_a = np.array([0.0])

        # SAC cont uses a squashed normal distribution. Implement it's logp
        # logic here in numpy for comparing results.
        def logp_func(means, log_stds, values, low=-1.0, high=1.0):
            stds = np.exp(np.clip(log_stds, MIN_LOG_NN_OUTPUT, MAX_LOG_NN_OUTPUT))
            unsquashed_values = np.arctanh((values - low) / (high - low) * 2.0 - 1.0)
            log_prob_unsquashed = np.sum(
                np.log(norm.pdf(unsquashed_values, means, stds)), -1
            )
            return log_prob_unsquashed - np.sum(
                np.log(1 - np.tanh(unsquashed_values) ** 2), axis=-1
            )

        do_test_log_likelihood(
            sac.SACTrainer,
            config,
            prev_a,
            continuous=True,
            layer_key=(
                "sequential/action",
                (0, 2),
                ("action_model.action_0.", "action_model.action_out."),
            ),
            logp_func=logp_func,
        )

    def test_sac_discr(self):
        """Tests SAC's (discrete actions) compute_log_likelihoods method."""
        config = sac.DEFAULT_CONFIG.copy()
        config["policy_model"]["fcnet_hiddens"] = [10]
        config["policy_model"]["fcnet_activation"] = "linear"
        prev_a = np.array(0)

        do_test_log_likelihood(
            sac.SACTrainer,
            config,
            prev_a,
            layer_key=(
                "sequential/action",
                (0, 2),
                ("action_model.action_0.", "action_model.action_out."),
            ),
        )


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
