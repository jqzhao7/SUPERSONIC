import json
import logging
import os

try:
    import pandas as pd
except ImportError:
    pd = None

from ray.tune.error import TuneError
from ray.tune.result import (
    EXPR_PROGRESS_FILE,
    EXPR_PARAM_FILE,
    CONFIG_PREFIX,
    TRAINING_ITERATION,
)
from ray.tune.trial import Trial
from ray.tune.trainable import TrainableUtil

logger = logging.getLogger(__name__)


class Analysis:
    """Analyze all results from a directory of experiments.

    To use this class, the experiment must be executed with the JsonLogger.
    """

    def __init__(self, experiment_dir):
        experiment_dir = os.path.expanduser(experiment_dir)
        if not os.path.isdir(experiment_dir):
            raise ValueError("{} is not a valid directory.".format(experiment_dir))
        self._experiment_dir = experiment_dir
        self._configs = {}
        self._trial_dataframes = {}

        if not pd:
            logger.warning(
                "pandas not installed. Run `pip install pandas` for "
                "Analysis utilities."
            )
        else:
            self.fetch_trial_dataframes()

    def dataframe(self, metric=None, mode=None):
        """Returns a pandas.DataFrame object constructed from the trials.

        Args:
            metric (str): Key for trial info to order on.
                If None, uses last result.
            mode (str): One of [min, max].

        Returns:
            pd.DataFrame: Constructed from a result dict of each trial.
        """
        rows = self._retrieve_rows(metric=metric, mode=mode)
        all_configs = self.get_all_configs(prefix=True)
        for path, config in all_configs.items():
            if path in rows:
                rows[path].update(config)
                rows[path].update(logdir=path)
        return pd.DataFrame(list(rows.values()))

    def get_best_config(self, metric, mode="max"):
        """Retrieve the best config corresponding to the trial.

        Args:
            metric (str): Key for trial info to order on.
            mode (str): One of [min, max].
        """
        rows = self._retrieve_rows(metric=metric, mode=mode)
        all_configs = self.get_all_configs()
        compare_op = max if mode == "max" else min
        best_path = compare_op(rows, key=lambda k: rows[k][metric])
        return all_configs[best_path]

    def get_best_logdir(self, metric, mode="max"):
        """Retrieve the logdir corresponding to the best trial.

        Args:
            metric (str): Key for trial info to order on.
            mode (str): One of [min, max].
        """
        df = self.dataframe(metric=metric, mode=mode)
        if mode == "max":
            return df.iloc[df[metric].idxmax()].logdir
        elif mode == "min":
            return df.iloc[df[metric].idxmin()].logdir

    def fetch_trial_dataframes(self):
        fail_count = 0
        for path in self._get_trial_paths():
            try:
                self.trial_dataframes[path] = pd.read_csv(
                    os.path.join(path, EXPR_PROGRESS_FILE)
                )
            except Exception:
                fail_count += 1

        if fail_count:
            logger.debug("Couldn't read results from {} paths".format(fail_count))
        return self.trial_dataframes

    def get_all_configs(self, prefix=False):
        """Returns a list of all configurations.

        Args:
            prefix (bool): If True, flattens the config dict
                and prepends `config/`.

        Returns:
            List[dict]: List of all configurations of trials,
        """
        fail_count = 0
        for path in self._get_trial_paths():
            try:
                with open(os.path.join(path, EXPR_PARAM_FILE)) as f:
                    config = json.load(f)
                    if prefix:
                        for k in list(config):
                            config[CONFIG_PREFIX + k] = config.pop(k)
                    self._configs[path] = config
            except Exception:
                fail_count += 1

        if fail_count:
            logger.warning("Couldn't read config from {} paths".format(fail_count))
        return self._configs

    def get_trial_checkpoints_paths(self, trial, metric=TRAINING_ITERATION):
        """Gets paths and metrics of all persistent checkpoints of a trial.

        Args:
            trial (Trial): The log directory of a trial, or a trial instance.
            metric (str): key for trial info to return, e.g. "mean_accuracy".
                "training_iteration" is used by default.

        Returns:
            List of [path, metric] for all persistent checkpoints of the trial.
        """
        if isinstance(trial, str):
            trial_dir = os.path.expanduser(trial)
            # Get checkpoints from logdir.
            chkpt_df = TrainableUtil.get_checkpoints_paths(trial_dir)

            # Join with trial dataframe to get metrics.
            trial_df = self.trial_dataframes[trial_dir]
            path_metric_df = chkpt_df.merge(
                trial_df, on="training_iteration", how="inner"
            )
            return path_metric_df[["chkpt_path", metric]].values.tolist()
        elif isinstance(trial, Trial):
            checkpoints = trial.checkpoint_manager.best_checkpoints()
            return [[c.value, c.result[metric]] for c in checkpoints]
        else:
            raise ValueError("trial should be a string or a Trial instance.")

    def _retrieve_rows(self, metric=None, mode=None):
        assert mode is None or mode in ["max", "min"]
        rows = {}
        for path, df in self.trial_dataframes.items():
            if mode == "max":
                idx = df[metric].idxmax()
            elif mode == "min":
                idx = df[metric].idxmin()
            else:
                idx = -1
            rows[path] = df.iloc[idx].to_dict()

        return rows

    def _get_trial_paths(self):
        _trial_paths = []
        for trial_path, _, files in os.walk(self._experiment_dir):
            if EXPR_PROGRESS_FILE in files:
                _trial_paths += [trial_path]

        if not _trial_paths:
            raise TuneError("No trials found in {}.".format(self._experiment_dir))
        return _trial_paths

    @property
    def trial_dataframes(self):
        """List of all dataframes of the trials."""
        return self._trial_dataframes


class ExperimentAnalysis(Analysis):
    """Analyze results from a Tune experiment.

    To use this class, the experiment must be executed with the JsonLogger.

    Parameters:
        experiment_checkpoint_path (str): Path to a json file
            representing an experiment state. Corresponds to
            Experiment.local_dir/Experiment.name/experiment_state.json
        trials (list|None): List of trials that can be accessed via
            `analysis.trials`.

    Example:
        >>> tune.run(my_trainable, name="my_exp", local_dir="~/tune_results")
        >>> analysis = ExperimentAnalysis(
        >>>     experiment_checkpoint_path="~/tune_results/my_exp/state.json")
    """

    def __init__(self, experiment_checkpoint_path, trials=None):
        with open(experiment_checkpoint_path) as f:
            _experiment_state = json.load(f)
            self._experiment_state = _experiment_state

        if "checkpoints" not in _experiment_state:
            raise TuneError("Experiment state invalid; no checkpoints found.")
        self._checkpoints = _experiment_state["checkpoints"]
        self.trials = trials
        super(ExperimentAnalysis, self).__init__(
            os.path.dirname(experiment_checkpoint_path)
        )

    def get_best_trial(self, metric, mode="max", scope="all"):
        """Retrieve the best trial object.

        Compares all trials' scores on `metric`.

        Args:
            metric (str): Key for trial info to order on.
            mode (str): One of [min, max].
            scope (str): One of [all, last, avg, last-5-avg, last-10-avg].
                If `scope=last`, only look at each trial's final step for
                `metric`, and compare across trials based on `mode=[min,max]`.
                If `scope=avg`, consider the simple average over all steps
                for `metric` and compare across trials based on
                `mode=[min,max]`. If `scope=last-5-avg` or `scope=last-10-avg`,
                consider the simple average over the last 5 or 10 steps for
                `metric` and compare across trials based on `mode=[min,max]`.
                If `scope=all`, find each trial's min/max score for `metric`
                based on `mode`, and compare trials based on `mode=[min,max]`.
        """
        if mode not in ["max", "min"]:
            raise ValueError(
                "ExperimentAnalysis: attempting to get best trial for "
                'metric {} for mode {} not in ["max", "min"]'.format(metric, mode)
            )
        if scope not in ["all", "last", "avg", "last-5-avg", "last-10-avg"]:
            raise ValueError(
                "ExperimentAnalysis: attempting to get best trial for "
                'metric {} for scope {} not in ["all", "last", "avg", '
                '"last-5-avg", "last-10-avg"]'.format(metric, scope)
            )
        best_trial = None
        best_metric_score = None
        for trial in self.trials:
            if metric not in trial.metric_analysis:
                continue

            if scope in ["last", "avg", "last-5-avg", "last-10-avg"]:
                metric_score = trial.metric_analysis[metric][scope]
            else:
                metric_score = trial.metric_analysis[metric][mode]

            if best_metric_score is None:
                best_metric_score = metric_score
                best_trial = trial
                continue

            if (mode == "max") and (best_metric_score < metric_score):
                best_metric_score = metric_score
                best_trial = trial
            elif (mode == "min") and (best_metric_score > metric_score):
                best_metric_score = metric_score
                best_trial = trial

        return best_trial

    def get_best_config(self, metric, mode="max", scope="all"):
        """Retrieve the best config corresponding to the trial.

        Compares all trials' scores on `metric`.

        Args:
            metric (str): Key for trial info to order on.
            mode (str): One of [min, max].
            scope (str): One of [all, last, avg, last-5-avg, last-10-avg].
                If `scope=last`, only look at each trial's final step for
                `metric`, and compare across trials based on `mode=[min,max]`.
                If `scope=avg`, consider the simple average over all steps
                for `metric` and compare across trials based on
                `mode=[min,max]`. If `scope=last-5-avg` or `scope=last-10-avg`,
                consider the simple average over the last 5 or 10 steps for
                `metric` and compare across trials based on `mode=[min,max]`.
                If `scope=all`, find each trial's min/max score for `metric`
                based on `mode`, and compare trials based on `mode=[min,max]`.
        """
        best_trial = self.get_best_trial(metric, mode, scope)
        return best_trial.config if best_trial else None

    def get_best_logdir(self, metric, mode="max", scope="all"):
        """Retrieve the logdir corresponding to the best trial.

        Compares all trials' scores on `metric`.

        Args:
            metric (str): Key for trial info to order on.
            mode (str): One of [min, max].
            scope (str): One of [all, last, avg, last-5-avg, last-10-avg].
                If `scope=last`, only look at each trial's final step for
                `metric`, and compare across trials based on `mode=[min,max]`.
                If `scope=avg`, consider the simple average over all steps
                for `metric` and compare across trials based on
                `mode=[min,max]`. If `scope=last-5-avg` or `scope=last-10-avg`,
                consider the simple average over the last 5 or 10 steps for
                `metric` and compare across trials based on `mode=[min,max]`.
                If `scope=all`, find each trial's min/max score for `metric`
                based on `mode`, and compare trials based on `mode=[min,max]`.
        """
        best_trial = self.get_best_trial(metric, mode, scope)
        return best_trial.logdir if best_trial else None

    def stats(self):
        """Returns a dictionary of the statistics of the experiment."""
        return self._experiment_state.get("stats")

    def runner_data(self):
        """Returns a dictionary of the TrialRunner data."""
        return self._experiment_state.get("runner_data")

    def _get_trial_paths(self):
        """Overwrites Analysis to only have trials of one experiment."""
        if self.trials:
            _trial_paths = [t.logdir for t in self.trials]
        else:
            logger.warning(
                "No `self.trials`. Drawing logdirs from checkpoint "
                "file. This may result in some information that is "
                "out of sync, as checkpointing is periodic."
            )
            _trial_paths = [checkpoint["logdir"] for checkpoint in self._checkpoints]
        if not _trial_paths:
            raise TuneError("No trials found.")
        return _trial_paths
