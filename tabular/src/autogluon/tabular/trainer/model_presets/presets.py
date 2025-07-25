from __future__ import annotations

import copy
import inspect
import logging
from collections import defaultdict

from packaging import version

from autogluon.common.model_filter import ModelFilter
from autogluon.common.utils.hyperparameter_utils import (
    get_deprecated_lightgbm_large_hyperparameters,
    get_hyperparameter_str_deprecation_msg,
)
from autogluon.core.constants import (
    AG_ARGS,
    AG_ARGS_ENSEMBLE,
    AG_ARGS_FIT,
    BINARY,
    MULTICLASS,
    QUANTILE,
    REGRESSION,
    SOFTCLASS,
)
from autogluon.core.models import (
    AbstractModel,
    StackerEnsembleModel,
)
from autogluon.core.trainer.utils import process_hyperparameters

from ...registry import ag_model_registry
from ...version import __version__

logger = logging.getLogger(__name__)

DEFAULT_CUSTOM_MODEL_PRIORITY = 0

VALID_AG_ARGS_KEYS = {
    "name",
    "name_main",
    "name_prefix",
    "name_suffix",
    "name_bag_suffix",
    "model_type",
    "priority",
    "problem_types",
    "disable_in_hpo",
    "valid_stacker",
    "valid_base",
    "hyperparameter_tune_kwargs",
}


# DONE: Add levels, including 'default'
# DONE: Add lists
# DONE: Add custom which can append to lists
# DONE: Add special optional AG args for things like name prefix, name suffix, name, etc.
# DONE: Move creation of stack ensemble internally into this function? Requires passing base models in as well.
# DONE: Add special optional AG args for training order
# DONE: Add special optional AG args for base models
# TODO: Consider making hyperparameters arg in fit() accept lists, concatenate hyperparameter sets together.
# TODO: Consider adding special optional AG args for #cores,#gpus,num_early_stopping_iterations,etc.
# DONE: Consider adding special optional AG args for max train time, max memory size, etc.
# TODO: Consider adding special optional AG args for use_original_features,features_to_use,etc.
# TODO: Consider adding optional AG args to dynamically disable models such as valid_num_classes_range, valid_row_count_range, valid_feature_count_range, etc.
# TODO: Args such as max_repeats, num_folds
# DONE: Add banned_model_types arg
# TODO: Add option to update hyperparameters with only added keys, so disabling CatBoost would just be {'CAT': []}, which keeps the other models as is.
# TODO: special optional AG arg for only training model if eval_metric in list / not in list. Useful for F1 and 'is_unbalanced' arg in LGBM.
def get_preset_models(
    path,
    problem_type,
    eval_metric,
    hyperparameters,
    level: int = 1,
    ensemble_type=StackerEnsembleModel,
    ensemble_kwargs: dict = None,
    ag_args_fit=None,
    ag_args=None,
    ag_args_ensemble=None,
    name_suffix: str = None,
    default_priorities=None,
    invalid_model_names: list = None,
    included_model_types: list = None,
    excluded_model_types: list = None,
    hyperparameter_preprocess_func=None,
    hyperparameter_preprocess_kwargs=None,
    silent=True,
):
    hyperparameters = process_hyperparameters(hyperparameters)
    if hyperparameter_preprocess_func is not None:
        if hyperparameter_preprocess_kwargs is None:
            hyperparameter_preprocess_kwargs = dict()
        hyperparameters = hyperparameter_preprocess_func(hyperparameters, **hyperparameter_preprocess_kwargs)
    if problem_type not in [BINARY, MULTICLASS, REGRESSION, SOFTCLASS, QUANTILE]:
        raise NotImplementedError
    invalid_name_set = set()
    if invalid_model_names is not None:
        invalid_name_set.update(invalid_model_names)

    if default_priorities is None:
        priority_cls_map = ag_model_registry.priority_map(problem_type=problem_type)
        default_priorities = {
            ag_model_registry.key(model_cls): priority for model_cls, priority in priority_cls_map.items()
        }

    level_key = level if level in hyperparameters.keys() else "default"
    if level_key not in hyperparameters.keys() and level_key == "default":
        hyperparameters = {"default": hyperparameters}
    hp_level = hyperparameters[level_key]
    hp_level = ModelFilter.filter_models(models=hp_level, included_model_types=included_model_types, excluded_model_types=excluded_model_types)
    model_cfg_priority_dict = defaultdict(list)
    model_type_list = list(hp_level.keys())
    for model_type in model_type_list:
        models_of_type = hp_level[model_type]
        if not isinstance(models_of_type, list):
            models_of_type = [models_of_type]
        model_cfgs_to_process = []
        for model_cfg in models_of_type:
            model_cfgs_to_process.append(model_cfg)
        for model_cfg in model_cfgs_to_process:
            model_cfg = clean_model_cfg(
                model_cfg=model_cfg,
                model_type=model_type,
                ag_args=ag_args,
                ag_args_ensemble=ag_args_ensemble,
                ag_args_fit=ag_args_fit,
                problem_type=problem_type,
            )
            model_cfg[AG_ARGS]["priority"] = model_cfg[AG_ARGS].get("priority", default_priorities.get(model_type, DEFAULT_CUSTOM_MODEL_PRIORITY))
            model_priority = model_cfg[AG_ARGS]["priority"]
            # Check if model_cfg is valid
            is_valid = is_model_cfg_valid(model_cfg, level=level, problem_type=problem_type)
            if AG_ARGS_FIT in model_cfg and not model_cfg[AG_ARGS_FIT]:
                model_cfg.pop(AG_ARGS_FIT)
            if is_valid:
                model_cfg_priority_dict[model_priority].append(model_cfg)

    model_cfg_priority_list = [model for priority in sorted(model_cfg_priority_dict.keys(), reverse=True) for model in model_cfg_priority_dict[priority]]

    if not silent:
        logger.log(20, "Model configs that will be trained (in order):")
    models = []
    model_args_fit = {}
    for model_cfg in model_cfg_priority_list:
        model = model_factory(
            model_cfg,
            path=path,
            problem_type=problem_type,
            eval_metric=eval_metric,
            name_suffix=name_suffix,
            ensemble_type=ensemble_type,
            ensemble_kwargs=ensemble_kwargs,
            invalid_name_set=invalid_name_set,
            level=level,
        )
        invalid_name_set.add(model.name)
        if "hyperparameter_tune_kwargs" in model_cfg[AG_ARGS]:
            model_args_fit[model.name] = {"hyperparameter_tune_kwargs": model_cfg[AG_ARGS]["hyperparameter_tune_kwargs"]}
        if "ag_args_ensemble" in model_cfg and not model_cfg["ag_args_ensemble"]:
            model_cfg.pop("ag_args_ensemble")
        if not silent:
            logger.log(20, f"\t{model.name}: \t{model_cfg}")
        models.append(model)
    return models, model_args_fit


def clean_model_cfg(model_cfg: dict, model_type=None, ag_args=None, ag_args_ensemble=None, ag_args_fit=None, problem_type=None):
    model_cfg = _verify_model_cfg(model_cfg=model_cfg, model_type=model_type)
    model_cfg = copy.deepcopy(model_cfg)
    if AG_ARGS not in model_cfg:
        model_cfg[AG_ARGS] = dict()
    if "model_type" not in model_cfg[AG_ARGS]:
        model_cfg[AG_ARGS]["model_type"] = model_type
    if model_cfg[AG_ARGS]["model_type"] is None:
        raise AssertionError(f"model_type was not specified for model! Model: {model_cfg}")
    model_type = model_cfg[AG_ARGS]["model_type"]
    model_types = ag_model_registry.key_to_cls_map()
    if not inspect.isclass(model_type):
        if model_type not in model_types:
            raise AssertionError(f"Unknown model type specified in hyperparameters: '{model_type}'. Valid model types: {list(model_types.keys())}")
        model_type = model_types[model_type]
    elif not issubclass(model_type, AbstractModel):
        logger.warning(
            f"Warning: Custom model type {model_type} does not inherit from {AbstractModel}. This may lead to instability. Consider wrapping {model_type} with an implementation of {AbstractModel}!"
        )
    else:
        if not ag_model_registry.exists(model_type):
            logger.log(20, f"Custom Model Type Detected: {model_type}")
    model_cfg[AG_ARGS]["model_type"] = model_type
    model_type_real = model_cfg[AG_ARGS]["model_type"]
    if not inspect.isclass(model_type_real):
        model_type_real = model_types[model_type_real]
    default_ag_args = model_type_real._get_default_ag_args()
    if ag_args is not None:
        model_extra_ag_args = ag_args.copy()
        model_extra_ag_args.update(model_cfg[AG_ARGS])
        model_cfg[AG_ARGS] = model_extra_ag_args
    default_ag_args_ensemble = model_type_real._get_default_ag_args_ensemble(problem_type=problem_type)
    if ag_args_ensemble is not None:
        model_extra_ag_args_ensemble = ag_args_ensemble.copy()
        model_extra_ag_args_ensemble.update(model_cfg.get(AG_ARGS_ENSEMBLE, dict()))
        model_cfg[AG_ARGS_ENSEMBLE] = model_extra_ag_args_ensemble
    if ag_args_fit is not None:
        if AG_ARGS_FIT not in model_cfg:
            model_cfg[AG_ARGS_FIT] = dict()
        model_extra_ag_args_fit = ag_args_fit.copy()
        model_extra_ag_args_fit.update(model_cfg[AG_ARGS_FIT])
        model_cfg[AG_ARGS_FIT] = model_extra_ag_args_fit
    if default_ag_args is not None:
        default_ag_args.update(model_cfg[AG_ARGS])
        model_cfg[AG_ARGS] = default_ag_args
    if default_ag_args_ensemble is not None:
        default_ag_args_ensemble.update(model_cfg.get(AG_ARGS_ENSEMBLE, dict()))
        model_cfg[AG_ARGS_ENSEMBLE] = default_ag_args_ensemble
    return model_cfg


def _verify_model_cfg(model_cfg, model_type) -> dict:
    """
    Ensures that model_cfg is of the correct type, or else raises an exception.
    Returns model_cfg
    """
    if not isinstance(model_cfg, dict):
        extra_msg = ""
        error = True
        if isinstance(model_cfg, str) and model_cfg == "GBMLarge":
            extra_msg = get_hyperparameter_str_deprecation_msg()
            if version.parse(__version__) >= version.parse("1.3.0"):
                error = True
                extra_msg = "\n" + extra_msg
            else:
                error = False
                model_cfg = get_deprecated_lightgbm_large_hyperparameters()
                logger.warning(
                    f"#######################################################"
                    f"\nWARNING: {extra_msg}"
                    f"\n#######################################################"
                )
        if error:
            raise AssertionError(
                f"Invalid model hyperparameters, expecting dict, but found {type(model_cfg)}! Model Type: {model_type} | Value: {model_cfg}{extra_msg}"
            )
    return model_cfg


# Check if model is valid
def is_model_cfg_valid(model_cfg, level=1, problem_type=None):
    is_valid = True
    for key in model_cfg.get(AG_ARGS, {}):
        if key not in VALID_AG_ARGS_KEYS:
            logger.warning(f"WARNING: Unknown ag_args key: {key}")
    if AG_ARGS not in model_cfg:
        is_valid = False  # AG_ARGS is required
    elif model_cfg[AG_ARGS].get("model_type", None) is None:
        is_valid = False  # model_type is required
    elif model_cfg[AG_ARGS].get("hyperparameter_tune_kwargs", None) and model_cfg[AG_ARGS].get("disable_in_hpo", False):
        is_valid = False
    elif not model_cfg[AG_ARGS].get("valid_stacker", True) and level > 1:
        is_valid = False  # Not valid as a stacker model
    elif not model_cfg[AG_ARGS].get("valid_base", True) and level == 1:
        is_valid = False  # Not valid as a base model
    elif problem_type is not None and problem_type not in model_cfg[AG_ARGS].get("problem_types", [problem_type]):
        is_valid = False  # Not valid for this problem_type
    return is_valid


def model_factory(
    model,
    path,
    problem_type,
    eval_metric,
    name_suffix=None,
    ensemble_type=StackerEnsembleModel,
    ensemble_kwargs=None,
    invalid_name_set=None,
    level=1,
):
    if invalid_name_set is None:
        invalid_name_set = set()
    model_type = model[AG_ARGS]["model_type"]
    if not inspect.isclass(model_type):
        model_type = ag_model_registry.key_to_cls(model_type)
    name_orig = model[AG_ARGS].get("name", None)
    if name_orig is None:
        ag_name = model_type.ag_name
        if ag_name is None:
            ag_name = model_type.__name__
        name_main = model[AG_ARGS].get("name_main", ag_name)
        name_prefix = model[AG_ARGS].get("name_prefix", "")
        name_suff = model[AG_ARGS].get("name_suffix", "")
        name_orig = name_prefix + name_main + name_suff
    name_stacker = None
    num_increment = 2
    if name_suffix is None:
        name_suffix = ""
    if ensemble_kwargs is None:
        name = f"{name_orig}{name_suffix}"
        while name in invalid_name_set:  # Ensure name is unique
            name = f"{name_orig}_{num_increment}{name_suffix}"
            num_increment += 1
    else:
        name = name_orig
        name_bag_suffix = model[AG_ARGS].get("name_bag_suffix", "_BAG")
        name_stacker = f"{name}{name_bag_suffix}_L{level}{name_suffix}"
        while name_stacker in invalid_name_set:  # Ensure name is unique
            name = f"{name_orig}_{num_increment}"
            name_stacker = f"{name}{name_bag_suffix}_L{level}{name_suffix}"
            num_increment += 1
    model_params = copy.deepcopy(model)
    model_params.pop(AG_ARGS, None)
    model_params.pop(AG_ARGS_ENSEMBLE, None)

    extra_ensemble_hyperparameters = copy.deepcopy(model.get(AG_ARGS_ENSEMBLE, dict()))

    # Enable user to pass ensemble hyperparameters via `"ag.ens.fold_fitting_strategy": "sequential_local"`
    ag_args_ensemble_prefix = "ag.ens."
    model_param_keys = list(model_params.keys())
    for key in model_param_keys:
        if key.startswith(ag_args_ensemble_prefix):
            key_suffix = key.split(ag_args_ensemble_prefix, 1)[-1]
            extra_ensemble_hyperparameters[key_suffix] = model_params.pop(key)

    model_init_kwargs = dict(
        path=path,
        name=name,
        problem_type=problem_type,
        eval_metric=eval_metric,
        hyperparameters=model_params,
    )

    if ensemble_kwargs is not None:
        ensemble_kwargs_model = copy.deepcopy(ensemble_kwargs)
        ensemble_kwargs_model["hyperparameters"] = ensemble_kwargs_model.get("hyperparameters", {})
        if ensemble_kwargs_model["hyperparameters"] is None:
            ensemble_kwargs_model["hyperparameters"] = {}
        ensemble_kwargs_model["hyperparameters"].update(extra_ensemble_hyperparameters)
        model_init = ensemble_type(path=path, name=name_stacker, model_base=model_type, model_base_kwargs=model_init_kwargs, **ensemble_kwargs_model)
    else:
        model_init = model_type(**model_init_kwargs)

    return model_init


# TODO: v0.1 cleanup and avoid hardcoded logic with model names
def get_preset_models_softclass(hyperparameters, invalid_model_names: list = None, **kwargs):
    from autogluon.core.metrics.softclass_metrics import soft_log_loss

    model_types_standard = ["GBM", "NN_TORCH", "CAT", "ENS_WEIGHTED"]
    hyperparameters = copy.deepcopy(hyperparameters)

    hyperparameters_standard = {key: hyperparameters[key] for key in hyperparameters if key in model_types_standard}
    hyperparameters_rf = {key: hyperparameters[key] for key in hyperparameters if key == "RF"}

    # Swap RF criterion for MSE:
    if "RF" in hyperparameters_rf:
        rf_params = hyperparameters_rf["RF"]
        rf_newparams = {"criterion": "squared_error", "ag_args": {"name_suffix": "MSE"}}
        for i in range(len(rf_params)):
            rf_params[i].update(rf_newparams)
        rf_params = [j for n, j in enumerate(rf_params) if j not in rf_params[(n + 1) :]]  # Remove duplicates which may arise after overwriting criterion
        hyperparameters_standard["RF"] = rf_params
    models, model_args_fit = get_preset_models(
        problem_type=SOFTCLASS,
        eval_metric=soft_log_loss,
        hyperparameters=hyperparameters_standard,
        invalid_model_names=invalid_model_names,
        **kwargs,
    )
    if len(models) == 0:
        raise ValueError(
            "At least one of the following model-types must be present in hyperparameters: ['GBM','CAT','RF'], "
            "These are the only supported models for softclass prediction problems. "
            "Softclass problems are also not yet supported for fit() with per-stack level hyperparameters."
        )
    for model in models:
        model.normalize_pred_probas = True  # FIXME: Do we need to do this for child models too?

    return models, model_args_fit
