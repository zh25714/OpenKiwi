#  OpenKiwi: Open-Source Machine Translation Quality Estimation
#  Copyright (C) 2020 Unbabel <openkiwi@unbabel.com>
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

import hydra
import joblib
import pydantic
from pydantic import FilePath, validator
from typing_extensions import Literal

import kiwi.cli
from kiwi import constants as const
from kiwi.lib import train
from kiwi.lib.utils import (
    configure_logging,
    configure_seed,
    load_config,
    save_config_to_file,
)
from kiwi.utils.io import BaseConfig

logger = logging.getLogger(__name__)


class RangeConfig(BaseConfig):
    """Specify a continuous interval, or a discrete range when step is set."""

    lower: float
    upper: float
    step: Optional[float]


class ClassWeightsConfig(BaseConfig):
    """Specify the range to search in for the tag loss weights."""

    target_tags: Union[None, List[float], RangeConfig] = RangeConfig(lower=1, upper=5)
    gap_tags: Union[None, List[float], RangeConfig] = RangeConfig(lower=1, upper=10)
    source_tags: Union[None, List[float], RangeConfig] = None


class SearchOptions(BaseConfig):
    patience: int = 10
    """Number of validations without improvement to wait before stopping training."""

    validation_steps: float = 0.2
    """Rely on the Kiwi training options to early stop bad models."""

    search_mlp: bool = False
    """To use or not to use an MLP after the encoder."""

    search_word_level: bool = False
    """Try with and without word level output. Useful to figure
    out if word level prediction is helping HTER regression performance."""

    search_hter: bool = False
    """Try with and without sentence level output. Useful to figure
    out if HTER regression is helping word level performance."""

    learning_rate: Union[None, List[float], RangeConfig] = RangeConfig(
        lower=5e-7, upper=5e-5
    )
    """Search the value for the learning rate."""

    dropout: Union[None, List[float], RangeConfig] = RangeConfig(lower=0.0, upper=0.3)
    """Search the dropout rate used in the decoder."""

    warmup_steps: Union[None, List[float], RangeConfig] = RangeConfig(
        lower=0.05, upper=0.4
    )
    """Search the number of steps to warm up the learning rate."""

    freeze_epochs: Union[None, List[float], RangeConfig] = RangeConfig(lower=0, upper=5)
    """Search the number of epochs to freeze the encoder."""

    class_weights: Union[None, ClassWeightsConfig] = ClassWeightsConfig()
    """Search the word-level tag loss weights."""

    sentence_loss_weight: Union[None, List[float], RangeConfig] = None
    """Search the weight to scale the sentence loss objective with."""

    hidden_size: Union[None, List[int], RangeConfig] = None
    """Search the hidden size of the MLP decoder."""

    bottleneck_size: Union[None, List[int], RangeConfig] = None
    """Search the size of the hidden layer in the decoder bottleneck."""

    search_method: Literal['random', 'tpe', 'multivariate_tpe'] = 'multivariate_tpe'
    """Use random search or the (multivariate) Tree-structured Parzen Estimator,
    or shorthand: TPE. See optuna.samplers for more details about these methods."""

    @validator('search_hter', pre=True, always=True)
    def check_consistency(cls, v, values):
        if v and values['search_word_level']:
            raise ValueError(
                'Cannot search both word level and sentence level '
                '(``options.search_hter=true`` and ``options.search_word_level=true``) '
                'because there will be no metric that covers both single objectives, '
                'and can lead to no training when neither of the output is selected; '
                'disable one or the other.'
            )
        else:
            return v


class Configuration(BaseConfig):
    base_config: Union[FilePath, train.Configuration]
    """Kiwi train configuration used as a base to configure the model.
    Can be a path or a yaml configuration properly indented under this argument."""

    directory: Path = Path('optunaruns')
    """Output directory."""

    seed: int = 42
    """Make the search reproducible."""

    search_name: str = None
    """The name used by the Optuna MLflow integration.
    If None, Optuna will create a unique hashed name."""

    num_trials: int = 50
    """The number of search trials to run."""

    num_models_to_keep: int = 5
    """The number of model checkpoints that are kept after training.
    The best ones are kept, the other ones removed to free up space.
    Keep all model checkpoints by setting this to -1."""

    options: SearchOptions = SearchOptions()
    """Configure the search method and parameter ranges."""

    load_study: FilePath = None
    """Continue from a previous saved study, i.e. from a `study.pkl` file."""

    verbose: bool = False
    quiet: bool = False

    @validator('base_config', pre=True, always=True)
    def parse_base_config(cls, v):
        if isinstance(v, dict):
            try:
                return train.Configuration(**v)
            except pydantic.error_wrappers.ValidationError as e:
                logger.info(str(e))
                if 'defaults' in str(e):
                    raise pydantic.ValidationError(
                        f'{e}. Configuration field `defaults` from the training '
                        'config is not supported in the search config; specify '
                        'the data fields in the regular way'
                    )
        else:
            return Path(v)


def search_from_file(filename):
    """Load options from a config file and calls the training procedure.

    Arguments:
        filename: of the configuration file.

    Return:
        an object with training information.
    """
    config = load_config(filename)
    return search_from_configuration(config)


def search_from_configuration(configuration_dict):
    """Run the entire training pipeline using the configuration options received.

    Arguments:
        configuration_dict: dictionary with options.

    Return: object with training information.
    """
    config = Configuration(**configuration_dict)

    study = run(config)

    return study


def get_suggestion(trial, param_name: str, config: Union[List, RangeConfig]):
    """Let the trial suggest a parameter value base on the range configuration."""
    if isinstance(config, list):
        return trial.suggest_categorical(param_name, config)
    elif config.step is not None:
        return trial.suggest_discrete_uniform(
            param_name, config.lower, config.upper, config.step
        )
    else:
        return trial.suggest_uniform(param_name, config.lower, config.upper)


class Objective:
    def __init__(self, config: Configuration, base_config_dict: dict):
        self.config = config
        self.base_config_dict = base_config_dict
        self.model_paths = []

    @property
    def main_metric(self) -> str:
        return 'val_' + '+'.join(self.base_config_dict['trainer']['main_metric'])

    @property
    def num_train_lines(self) -> int:
        return sum(
            1 for _ in open(self.base_config_dict['data']['train']['input']['source'])
        )

    @property
    def updates_per_epochs(self) -> int:
        return int(
            self.num_train_lines
            / self.base_config_dict['system']['batch_size']['train']
            / self.base_config_dict['trainer']['gradient_accumulation_steps']
        )

    @property
    def best_model_paths(self) -> List[Path]:
        """Return the model paths sorted from high to low by their objective score."""
        return list(
            tuple(zip(*sorted(self.model_paths, key=lambda t: t[1], reverse=True)))[0]
        )

    def suggest_train_config(self, trial) -> Tuple[train.Configuration, dict]:
        """Use the trial to suggest values to initialize a training configuration."""
        base_config_dict = deepcopy(self.base_config_dict)

        # Compute the training steps from the training data and set in the base config
        base_config_dict['system']['optimizer']['training_steps'] = (
            self.updates_per_epochs
        ) * base_config_dict['trainer']['epochs']

        # Collect the values to optimize
        search_values = {}
        # Suggest a learning rate
        if self.config.options.learning_rate is not None:
            learning_rate = get_suggestion(
                trial, 'learning_rate', self.config.options.learning_rate
            )
            base_config_dict['system']['optimizer']['learning_rate'] = learning_rate
            search_values['learning_rate'] = learning_rate
        # Suggest a dropout probability
        if self.config.options.dropout is not None:
            dropout = get_suggestion(trial, 'dropout', self.config.options.dropout)
            base_config_dict['system']['model']['outputs']['dropout'] = dropout
            if (
                base_config_dict['system']['model']['encoder'].get('dropout')
                is not None
            ):
                base_config_dict['system']['model']['encoder']['dropout'] = dropout
            if (
                base_config_dict['system']['model']['decoder'].get('dropout')
                is not None
            ):
                base_config_dict['system']['model']['decoder']['dropout'] = dropout
            if base_config_dict['system']['model']['decoder'].get('source') is not None:
                base_config_dict['system']['model']['decoder']['source'][
                    'dropout'
                ] = dropout
            if base_config_dict['system']['model']['decoder'].get('target') is not None:
                base_config_dict['system']['model']['decoder']['target'][
                    'dropout'
                ] = dropout
            search_values['dropout'] = dropout
        # Suggest the number of warmup steps
        if self.config.options.warmup_steps is not None:
            warmup_steps = get_suggestion(
                trial, 'warmup_steps', self.config.options.warmup_steps
            )
            base_config_dict['system']['optimizer']['warmup_steps'] = warmup_steps
            search_values['warmup_steps'] = warmup_steps
        # Suggest the number of freeze epochs
        if self.config.options.freeze_epochs is not None:
            freeze_epochs = get_suggestion(
                trial, 'freeze_epochs', self.config.options.freeze_epochs
            )
            base_config_dict['system']['model']['encoder']['freeze'] = True
            base_config_dict['system']['model']['encoder'][
                'freeze_for_number_of_steps'
            ] = int(self.updates_per_epochs * freeze_epochs)
            search_values['freeze_epochs'] = freeze_epochs
        # Suggest a hidden size
        if self.config.options.hidden_size is not None:
            hidden_size = get_suggestion(
                trial, 'hidden_size', self.config.options.hidden_size
            )
            base_config_dict['system']['model']['encoder']['hidden_size'] = hidden_size
            base_config_dict['system']['model']['decoder']['hidden_size'] = hidden_size
            search_values['hidden_size'] = hidden_size
        # Suggest a bottleneck size
        if self.config.options.bottleneck_size is not None:
            bottleneck_size = get_suggestion(
                trial, 'bottleneck_size', self.config.options.bottleneck_size
            )
            base_config_dict['system']['model']['decoder'][
                'bottleneck_size'
            ] = bottleneck_size
            search_values['bottleneck_size'] = bottleneck_size
        # Suggest whether to use the MLP after the encoder
        if self.config.options.search_mlp:
            use_mlp = trial.suggest_categorical('mlp', [True, False])
            base_config_dict['system']['model']['encoder']['use_mlp'] = use_mlp
            search_values['use_mlp'] = use_mlp

        # Search word_level and sentence_level and their combinations
        # Suggest whether to include the sentence level objective
        if self.config.options.search_hter:
            assert (
                base_config_dict['data']['train']['output']['sentence_scores']
                is not None
            )
            assert (
                base_config_dict['data']['valid']['output']['sentence_scores']
                is not None
            )
            hter = trial.suggest_categorical('hter', [True, False])
            base_config_dict['system']['model']['outputs']['sentence_level'][
                'hter'
            ] = hter
            search_values['hter'] = hter
        # Suggest whether to include the word level objective
        if self.config.options.search_word_level:
            assert (
                base_config_dict['data']['train']['output']['target_tags'] is not None
            )
            assert (
                base_config_dict['data']['valid']['output']['target_tags'] is not None
            )
            word_level = trial.suggest_categorical('word_level', [True, False])
            base_config_dict['system']['model']['outputs']['word_level'][
                'target'
            ] = word_level
            base_config_dict['system']['model']['outputs']['word_level'][
                'gaps'
            ] = word_level
            search_values['word_level'] = word_level

        # We search for `sentence_loss_weight` when there is both a word level
        #   objective and a sentence level objective. Otherwise it does not matter
        #   to weigh the loss of one objective: for the loss it's the ratio between
        #   the two objectives that matters, not the absolute value of one loss
        #   separately.
        word_level_config = base_config_dict['system']['model']['outputs']['word_level']
        sentence_level_config = base_config_dict['system']['model']['outputs'][
            'sentence_level'
        ]
        if (
            word_level_config.get('target')
            and sentence_level_config.get('hter')
            and self.config.options.sentence_loss_weight
        ):
            sentence_loss_weight = get_suggestion(
                trial, 'sentence_loss_weight', self.config.options.sentence_loss_weight
            )
            base_config_dict['system']['model']['outputs'][
                'sentence_loss_weight'
            ] = sentence_loss_weight
            search_values['sentence_loss_weight'] = sentence_loss_weight

        # The same logic applies here: when search whether to include the sentence level
        #   objective (`search_hter`) then we will only want to search the
        #   `sentence_loss_weight` when we are actually including the sentence level
        #   objevtive (`hter = True`). See the above definition of the variable `hter`.
        if (
            word_level_config.get('target')
            and self.config.options.search_hter
            and self.config.options.sentence_loss_weight
        ):
            if hter:
                sentence_loss_weight = get_suggestion(
                    trial,
                    'sentence_loss_weight',
                    self.config.options.sentence_loss_weight,
                )
                base_config_dict['system']['model']['outputs'][
                    'sentence_loss_weight'
                ] = sentence_loss_weight
                search_values['sentence_loss_weight'] = sentence_loss_weight

        if self.config.options.class_weights and word_level_config:
            # if self.config.options.search_word_level:
            #     if not word_level:
            #         # Word level disabled; nothing to search over
            #         pass
            if (
                self.config.options.class_weights.target_tags
                and word_level_config['target']
            ):
                class_weight_target_tags = get_suggestion(
                    trial,
                    'class_weight_target_tags',
                    self.config.options.class_weights.target_tags,
                )
                base_config_dict['system']['model']['outputs']['word_level'][
                    'class_weights'
                ]['target_tags'] = {const.BAD: class_weight_target_tags}
                search_values['class_weight_target_tags'] = class_weight_target_tags
            if self.config.options.class_weights.gap_tags and word_level_config['gaps']:
                class_weight_gap_tags = get_suggestion(
                    trial,
                    'class_weight_gap_tags',
                    self.config.options.class_weights.gap_tags,
                )
                base_config_dict['system']['model']['outputs']['word_level'][
                    'class_weights'
                ]['gap_tags'] = {const.BAD: class_weight_gap_tags}
                search_values['class_weight_gap_tags'] = class_weight_gap_tags
            if (
                self.config.options.class_weights.source_tags
                and word_level_config['source']
            ):
                class_weight_source_tags = get_suggestion(
                    trial,
                    'class_weight_source_tags',
                    self.config.options.class_weights.source_tags,
                )
                base_config_dict['system']['model']['outputs']['word_level'][
                    'class_weights'
                ]['source_tags'] = {const.BAD: class_weight_source_tags}
                search_values['class_weight_source_tags'] = class_weight_source_tags
        return train.Configuration(**base_config_dict), search_values

    def __call__(self, trial) -> float:
        # TODO: integrate PTL trainer callback
        # ptl_callback = PyTorchLightningPruningCallback(trial, monitor='val_PEARSON')
        # train_info = train.run(trainconfig, ptl_callback=ptl_callback)

        train_config, search_values = self.suggest_train_config(trial)

        logger.info(f'############# STARTING TRIAL {trial.number} #############')
        logger.info(f'PARAMETERS: {search_values}')
        for name, value in search_values.items():
            logger.info(f'{name}: {value}')

        try:
            train_info = train.run(train_config)
        except RuntimeError as e:
            logger.info(f'ERROR OCCURED; SKIPPING TRIAL: {e}')
            return -1

        logger.info(f'############# TRIAL {trial.number} FINISHED #############')
        result = train_info.best_metrics.get(self.main_metric, -1)
        if result == -1:
            logger.error(
                'Trial did not converge:\n'
                f'    `train_info.best_metrics={train_info.best_metrics}`\n'
                f'Setting result to -1'
            )

        logger.info(f'RESULTS: {result} {self.main_metric}')
        logger.info(f'MODEL: {train_info.best_model_path}')
        logger.info(f'PARAMETERS: {search_values}')
        for name, value in search_values.items():
            logger.info(f'{name}: {value}')

        # Store the checkpoint path for later access
        self.model_paths.append((train_info.best_model_path, result))

        return result


def setup_run(directory: Path, seed: int, debug=False, quiet=False) -> Path:
    # TODO: replace all of this with the MLFlow integration: optuna.integration.mlflow
    #   In particular: let the output directory be created by MLflow entirely
    if not directory.exists():
        # Initialize a new folder with name '0'
        output_dir = directory / '0'
        output_dir.mkdir(parents=True)
    else:
        # Initialize a new folder incrementing folder count
        output_dir = directory / str(len(list(directory.glob('*'))))
        if output_dir.exists():
            backup_directory = output_dir.with_name(
                f'{output_dir.name}_backup_{datetime.now().isoformat()}'
            )
            logger.warning(
                f'Folder {output_dir} already exists; moving it to {backup_directory}'
            )
            output_dir.rename(backup_directory)
        output_dir.mkdir(parents=True)

    logger.info(f'Initializing new search folder at: {output_dir}')

    configure_logging(output_dir=output_dir, verbose=debug, quiet=quiet)
    configure_seed(seed)

    return output_dir


def run(config: Configuration):
    try:
        import optuna
    except ImportError as e:
        raise ImportError(
            f'{e}. Install the search dependencies with '
            '``pip install -U openkiwi[search]``, or with '
            '``poetry install -E search`` when setting '
            'up for local development'
        )

    output_dir = setup_run(config.directory, config.seed)
    logger.info(f'Saving all Optuna results to: {output_dir}')

    if isinstance(config.base_config, Path):
        # FIXME: this is not neat (but it does work)
        #   We need this in order to support the `defaults` field in the train config,
        #   like the
        #     defaults:
        #        - data: wmt19.qe.en_de
        #   in the config/xlmroberta. It's Hydra that takes care of that inside
        #   `kiwi.cli.arguments_to_configuration`. And because Hydra has already
        #   been configured globally in the Kiwi cli, we need to clear it.
        hydra._internal.hydra.GlobalHydra().clear()
        base_dict = kiwi.cli.arguments_to_configuration(
            {'CONFIG_FILE': config.base_config}
        )
        # These arguments are not in the train configuration because they are
        #   added by `kiwi.cli.arguments_to_configuration` (and we remove them
        #   so Pydantic won't throw an error.)
        del base_dict['verbose'], base_dict['quiet']
        base_config = train.Configuration(**base_dict)
    else:
        base_config = config.base_config
    base_config_dict = base_config.dict()

    # Perform some checks of the training config
    use_mlflow = True if base_config_dict['run'].get('use_mlflow') else False
    if not use_mlflow:
        logger.warning(
            'Using MLflow is recommend; set `run.use_mlflow=true` in the base config'
        )
    # The main metric should be explicitly set
    if base_config_dict['trainer']['main_metric'] is None:
        raise ValueError(
            'The metric should be explicitly set in `trainer.main_metric` '
            'in the training config (`base_config`).'
        )
    # The main metric(s) should be inside a list
    if not isinstance(base_config_dict['trainer']['main_metric'], list):
        base_config_dict['trainer']['main_metric'] = [
            base_config_dict['trainer']['main_metric']
        ]

    # Use the early stopping logic of the Kiwi trainer
    base_config_dict['trainer']['checkpoint'][
        'early_stop_patience'
    ] = config.options.patience
    base_config_dict['trainer']['checkpoint'][
        'validation_steps'
    ] = config.options.validation_steps

    # Load or initialize a study
    if config.load_study:
        logger.info(f'Loading study to resume from: {config.load_study}')
        study = joblib.load(config.load_study)
    else:
        if config.options.search_method == 'random':
            logger.info('Exploring parameters with random sampler')
            sampler = optuna.samplers.RandomSampler(seed=config.seed)
        else:
            multivariate = (config.options.search_method == 'multivariate_tpe',)
            logger.info(
                'Exploring parameters with '
                f'{"multivariate " if multivariate else ""}TPE sampler'
            )
            sampler = optuna.samplers.TPESampler(
                seed=config.seed, multivariate=multivariate,
            )
        logger.info('Initializing study...')
        pruner = optuna.pruners.MedianPruner()
        study = optuna.create_study(
            study_name=config.search_name,
            direction='maximize',
            pruner=pruner,
            sampler=sampler,
        )

    # Optimize the study
    optimize_kwargs = {}
    if use_mlflow:
        optimize_kwargs['callbacks'] = [optuna.integration.MLflowCallback()]
    try:
        logger.info('Optimizing study...')
        objective = Objective(config=config, base_config_dict=base_config_dict)
        study.optimize(
            objective, n_trials=config.num_trials, **optimize_kwargs,
        )
    except KeyboardInterrupt:
        logger.info('Early stopping search caused by ctrl-C')
    except Exception as e:
        logger.error(
            f'Error occured during search: {e}; '
            f'current best params are: {study.best_params}'
        )

    logger.info(f"Saving study to: {output_dir / 'study.pkl'}")
    joblib.dump(study, output_dir / 'study.pkl')

    try:
        logger.info('Number of finished trials: {}'.format(len(study.trials)))
        logger.info('Best trial:')
        trial = study.best_trial
        logger.info('  Value: {}'.format(trial.value))
        logger.info('  Params: ')
        for key, value in trial.params.items():
            logger.info('    {}: {}'.format(key, value))
    except Exception as e:
        logger.error(f'Logging at end of search failed: {e}')

    if config.num_models_to_keep > 0 and config.num_trials > config.num_models_to_keep:
        # Keep only n best model checkpoints and remove the rest to free up space
        try:
            best_model_paths = objective.best_model_paths
            models_to_keep = best_model_paths[: config.num_models_to_keep]
            models_to_remove = best_model_paths[config.num_models_to_keep :]
            logger.info(
                f'Keeping the {config.num_models_to_keep} best models '
                'from the search and removing the other ones.'
            )
            logger.info('Models that are kept:')
            for path in models_to_keep:
                logger.info(f'\t{path}')
            for path in models_to_remove:
                Path(path).unlink()
        except Exception as e:
            logger.error(f'Logging at end of search failed: {e}')

    logger.info(f'Saving Optuna plots for this search to: {output_dir}')
    save_config_to_file(config, output_dir / 'search_config.yaml')
    try:
        fig = optuna.visualization.plot_optimization_history(study)
        fig.write_html(str(output_dir / 'optimization_history.html'))
    except Exception as e:
        logger.error(f'Failed to create plot `optimization_history`: {e}')

    try:
        fig = optuna.visualization.plot_parallel_coordinate(
            study, params=list(trial.params.keys())
        )
        fig.write_html(str(output_dir / 'parallel_coordinate.html'))
    except Exception as e:
        logger.error(f'Failed to create plot `parallel_coordinate`: {e}')

    try:
        # This fits a random forest regression model using sklearn to predict the
        #   importance of each parameter
        fig = optuna.visualization.plot_param_importances(study)
        fig.write_html(str(output_dir / 'param_importances.html'))
    except Exception as e:
        logger.error(f'Failed to create plot `param_importances`: {e}')

    return study
