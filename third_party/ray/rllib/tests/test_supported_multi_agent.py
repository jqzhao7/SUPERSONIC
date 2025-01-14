import unittest

import ray
from ray.rllib.agents.registry import get_agent_class
from ray.rllib.examples.env.multi_agent import MultiAgentCartPole, MultiAgentMountainCar
from ray.rllib.utils.test_utils import framework_iterator
from ray.tune import register_env


def check_support_multiagent(alg, config):
    register_env(
        "multi_agent_mountaincar", lambda _: MultiAgentMountainCar({"num_agents": 2})
    )
    register_env(
        "multi_agent_cartpole", lambda _: MultiAgentCartPole({"num_agents": 2})
    )
    config["log_level"] = "ERROR"
    for _ in framework_iterator(config, frameworks=("torch", "tf")):
        if alg in ["DDPG", "APEX_DDPG", "SAC"]:
            a = get_agent_class(alg)(config=config, env="multi_agent_mountaincar")
        else:
            a = get_agent_class(alg)(config=config, env="multi_agent_cartpole")
        try:
            print(a.train())
        finally:
            a.stop()


class TestSupportedMultiAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ray.init(num_cpus=4)

    @classmethod
    def tearDownClass(cls) -> None:
        ray.shutdown()

    def test_a3c_multiagent(self):
        check_support_multiagent(
            "A3C", {"num_workers": 1, "optimizer": {"grads_per_step": 1}}
        )

    def test_apex_multiagent(self):
        check_support_multiagent(
            "APEX",
            {
                "num_workers": 2,
                "timesteps_per_iteration": 100,
                "num_gpus": 0,
                "buffer_size": 1000,
                "min_iter_time_s": 1,
                "learning_starts": 10,
                "target_network_update_freq": 100,
            },
        )

    def test_apex_ddpg_multiagent(self):
        check_support_multiagent(
            "APEX_DDPG",
            {
                "num_workers": 2,
                "timesteps_per_iteration": 100,
                "buffer_size": 1000,
                "num_gpus": 0,
                "min_iter_time_s": 1,
                "learning_starts": 10,
                "target_network_update_freq": 100,
                "use_state_preprocessor": True,
            },
        )

    def test_ddpg_multiagent(self):
        check_support_multiagent(
            "DDPG",
            {
                "timesteps_per_iteration": 1,
                "buffer_size": 1000,
                "use_state_preprocessor": True,
                "learning_starts": 500,
            },
        )

    def test_dqn_multiagent(self):
        check_support_multiagent(
            "DQN", {"timesteps_per_iteration": 1, "buffer_size": 1000,}
        )

    def test_impala_multiagent(self):
        check_support_multiagent("IMPALA", {"num_gpus": 0})

    def test_pg_multiagent(self):
        check_support_multiagent("PG", {"num_workers": 1, "optimizer": {}})

    def test_ppo_multiagent(self):
        check_support_multiagent(
            "PPO",
            {
                "num_workers": 1,
                "num_sgd_iter": 1,
                "train_batch_size": 10,
                "rollout_fragment_length": 10,
                "sgd_minibatch_size": 1,
            },
        )

    def test_sac_multiagent(self):
        check_support_multiagent(
            "SAC", {"num_workers": 0, "buffer_size": 1000, "normalize_actions": False,}
        )


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
