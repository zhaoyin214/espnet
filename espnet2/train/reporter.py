from __future__ import annotations

import dataclasses
import datetime
import logging
import time
import warnings
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import ContextManager
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

import humanfriendly
import numpy as np
import torch
from typeguard import check_argument_types
from typeguard import check_return_type

Num = Union[float, int, complex, torch.Tensor, np.ndarray]


_reserved = {"time", "total_count"}


def to_reported_value(v: Num, weight: Num = None) -> ReportedValue:
    assert check_argument_types()
    if isinstance(v, (torch.Tensor, np.ndarray)):
        if np.prod(v.shape) != 1:
            raise ValueError(f"v must be 0 or 1 dimension: {len(v.shape)}")
        v = v.item()

    if isinstance(weight, (torch.Tensor, np.ndarray)):
        if np.prod(weight.shape) != 1:
            raise ValueError(f"weight must be 0 or 1 dimension: {len(weight.shape)}")
        weight = weight.item()

    if weight is not None:
        retval = WeightedAverage(v, weight)
    else:
        retval = Average(v)
    assert check_return_type(retval)
    return retval


def aggregate(values: Sequence[ReportedValue]) -> Num:
    assert check_argument_types()

    for v in values:
        if not isinstance(v, type(values[0])):
            raise ValueError(
                f"Can't use different Reported type together: "
                f"{type(v)} != {type(values[0])}"
            )

    if len(values) == 0:
        warnings.warn("No stats found")
        retval = np.nan

    elif isinstance(values[0], Average):
        retval = np.nanmean([v.value for v in values])

    elif isinstance(values[0], WeightedAverage):
        # Excludes non finite values
        invalid_indices = set()
        for i, v in enumerate(values):
            if not np.isfinite(v.value) or not np.isfinite(v.weight):
                invalid_indices.add(i)
        values = [v for i, v in enumerate(values) if i not in invalid_indices]

        if len(values) != 0:
            # Calc weighed average. Weights are changed to sum-to-1.
            sum_weights = sum(v.weight for i, v in enumerate(values))
            sum_value = sum(v.value * v.weight for i, v in enumerate(values))
            if sum_weights == 0:
                warnings.warn("weight is zero")
                retval = np.nan
            else:
                retval = sum_value / sum_weights
        else:
            warnings.warn("No valid stats found")
            retval = np.nan

    else:
        raise NotImplementedError(f"type={type(values[0])}")
    assert check_return_type(retval)
    return retval


class ReportedValue:
    pass


@dataclasses.dataclass(frozen=True)
class Average(ReportedValue):
    value: Num


@dataclasses.dataclass(frozen=True)
class WeightedAverage(ReportedValue):
    value: Tuple[Num, Num]
    weight: Num


class SubReporter:
    """This class is used in Reporter

    See the docstring of Reporter for the usage.
    """

    def __init__(self, key: str, epoch: int, total_count: int):
        assert check_argument_types()
        self.key = key
        self.epoch = epoch
        self.start_time = time.perf_counter()
        self.stats = defaultdict(list)
        self.total_count = total_count
        self._finished = False

    def get_total_count(self) -> int:
        """Returns the number of iterations over all epochs"""
        return self.total_count

    def register(
        self,
        stats: Dict[str, Optional[Union[Num, Dict[str, Num]]]],
        weight: Num = None,
        not_increment_count: bool = False,
    ) -> None:
        assert check_argument_types()
        if self._finished:
            raise RuntimeError("Already finished")
        if not not_increment_count:
            self.total_count += 1

        for key2, v in stats.items():
            if key2 in _reserved:
                raise RuntimeError(f"{key2} is reserved.")
            # if None value, the key is not registered
            if v is None:
                continue
            r = to_reported_value(v, weight)
            self.stats[key2].append(r)

    def logging(self, logger=None, level: str = "INFO", nlatest: int = None) -> None:
        if self._finished:
            raise RuntimeError("Already finished")
        if logger is None:
            logger = logging
        level = logging.getLevelName(level)

        if nlatest is None:
            nlatest = 0

        message = ""
        for key2, stats in self.stats.items():
            # values: List[ReportValue]
            values = stats[-nlatest:]
            if len(message) == 0:
                message += (
                    f"{self.epoch}epoch:{self.key}:"
                    f"{len(stats) - nlatest + 1}-{len(stats)}batch: "
                )
            else:
                message += ", "

            v = aggregate(values)
            message += f"{key2}={v:.3f}"
        logger.log(level, message)

    def finished(self) -> None:
        self._finished = True


class Reporter:
    """

    Examples:

        >>> reporter = Reporter()
        >>> with reporter.observe('train') as sub_reporter:
        ...     for batch in iterator:
        ...         stats = dict(loss=0.2)
        ...         sub_reporter.register(stats)

    """

    def __init__(self, epoch: int = 0):
        assert check_argument_types()
        if epoch < 0:
            raise ValueError(f"epoch must be 0 or more: {epoch}")
        self.epoch = epoch
        # stats: Dict[int, Dict[str, Dict[str, float]]]
        # e.g. self.stats[epoch]['train']['loss']
        self.stats = defaultdict(dict)

    def get_epoch(self) -> int:
        return self.epoch

    def set_epoch(self, epoch: int) -> None:
        if epoch < 0:
            raise ValueError(f"epoch must be 0 or more: {epoch}")
        self.epoch = epoch

    @contextmanager
    def observe(self, key: str, epoch: int = None) -> ContextManager[SubReporter]:
        sub_reporter = self.start_epoch(key, epoch)
        yield sub_reporter
        # Receive the stats from sub_reporter
        self.finish_epoch(sub_reporter)

    def start_epoch(self, key: str, epoch: int = None) -> SubReporter:
        if epoch is not None:
            if epoch < 0:
                raise ValueError(f"epoch must be 0 or more: {epoch}")
            self.epoch = epoch

        if self.epoch - 1 not in self.stats or key not in self.stats[self.epoch - 1]:
            # If the previous epoch doesn't exist for some reason,
            # maybe due to bug, this case also indicates 0-count.
            if self.epoch - 1 != 0:
                warnings.warn(
                    f"The stats of the previous epoch={self.epoch - 1}"
                    f"doesn't exist."
                )
            total_count = 0
        else:
            total_count = self.stats[self.epoch - 1][key]["total_count"]

        sub_reporter = SubReporter(key, self.epoch, total_count)
        # Clear the stats for the next epoch if it exists
        self.stats.pop(epoch, None)
        return sub_reporter

    def finish_epoch(self, sub_reporter: SubReporter) -> None:
        if self.epoch != sub_reporter.epoch:
            raise RuntimeError(
                f"Don't change epoch during observation: "
                f"{self.epoch} != {sub_reporter.epoch}"
            )

        # Calc mean of current stats and set it as previous epochs stats
        stats = {}
        for key2, values in sub_reporter.stats.items():
            v = aggregate(values)
            stats[key2] = v

        stats["time"] = datetime.timedelta(
            seconds=time.perf_counter() - sub_reporter.start_time
        )
        stats["total_count"] = sub_reporter.total_count

        self.stats[self.epoch][sub_reporter.key] = stats
        sub_reporter.finished()

    def sort_epochs_and_values(
        self, key: str, key2: str, mode: str
    ) -> List[Tuple[int, float]]:
        """Return the epoch which resulted the best value
        Example:
            >>> val = reporter.sort_epochs_and_values('eval', 'loss', 'min')
            >>> e_1best, v_1best = val[0]
            >>> e_2best, v_2best = val[1]
        """
        if mode not in ("min", "max"):
            raise ValueError(f"mode must min or max: {mode}")

        # iterate from the last epoch
        values = [(e, self.stats[e][key][key2]) for e in self.stats]

        if mode == "min":
            values = sorted(values, key=lambda x: x[1])
        else:
            values = sorted(values, key=lambda x: -x[1])
        return values

    def has(self, key: str, key2: str, epoch: int = None) -> bool:
        if epoch is None:
            epoch = max(self.stats)
        return (
            epoch in self.stats
            and key in self.stats[epoch]
            and key2 in self.stats[epoch][key]
        )

    def logging(self, logger=None, level: str = "INFO", epoch: int = None):
        if logger is None:
            logger = logging
        if epoch is None:
            epoch = max(self.stats)
        level = logging.getLevelName(level)

        message = ""
        for key, d in self.stats[epoch].items():
            _message = ""
            for key2, v in d.items():
                if v is not None:
                    if len(_message) != 0:
                        _message += ", "
                    if isinstance(v, float):
                        _message += f"{key2}={v:.3f}"
                    elif isinstance(v, datetime.timedelta):
                        _v = humanfriendly.format_timespan(v)
                        _message += f"{key2}={_v}"
                    else:
                        _message += f"{key2}={v}"
            if len(_message) != 0:
                if len(message) == 0:
                    message += f"{epoch}epoch results: "
                else:
                    message += ", "
                message += f"[{key}] {_message}"
        logger.log(level, message)

    def get_value(self, key: str, key2: str, epoch: int = None):
        if epoch is None:
            epoch = max(self.stats)
        return self.stats[epoch][key][key2]

    def get_keys(self, epoch: int = None) -> Tuple[str, ...]:
        if epoch is None:
            epoch = max(self.stats)
        return tuple(self.stats[epoch])

    def get_keys2(self, key: str, epoch: int = None) -> Tuple[str, ...]:
        if epoch is None:
            epoch = max(self.stats)
        d = self.stats[epoch][key]
        keys2 = tuple(k for k in d if k not in ("time", "total_count"))
        return keys2

    def get_all_keys(self, epoch: int = None) -> Tuple[Tuple[str, str], ...]:
        if epoch is None:
            epoch = max(self.stats)
        all_keys = []
        for key in self.stats[epoch]:
            for key2 in self.stats[epoch][key]:
                all_keys.append((key, key2))
        return tuple(all_keys)

    def save_stats_plot(self, output_dir: Union[str, Path]):
        """Plot stats using Matplotlib and save images"""
        keys2 = set.union(*[set(self.get_keys2(k)) for k in self.get_keys()])
        for key2 in keys2:
            keys = [k for k in self.get_keys() if key2 in self.get_keys2(k)]
            plt = self._plot_stats(keys, key2)
            p = output_dir / f"{key2}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(p)

    def _plot_stats(self, keys: Sequence[str], key2: str):
        assert check_argument_types()
        # str is also Sequence[str]
        if isinstance(keys, str):
            raise TypeError(f"Input as [{keys}]")

        import matplotlib

        matplotlib.use("agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker

        plt.clf()

        epochs = np.arange(1, max(self.stats) + 1)
        for key in keys:
            y = [
                np.nanmean(self.stats[e][key][key2])
                if e in self.stats
                and key in self.stats[e]
                and key2 in self.stats[e][key]
                else np.nan
                for e in epochs
            ]
            assert len(epochs) == len(y), "Bug?"

            plt.plot(epochs, y, label=key, marker="x")
        plt.legend()
        plt.title(f"epoch vs {key2}")
        # Force integer tick for x-axis
        plt.gca().get_xaxis().set_major_locator(ticker.MaxNLocator(integer=True))
        plt.xlabel("epoch")
        plt.ylabel(key2)
        plt.grid()

        return plt

    def state_dict(self):
        return {"stats": self.stats, "epoch": self.epoch}

    def load_state_dict(self, state_dict: dict):
        self.epoch = state_dict["epoch"]
        self.stats = state_dict["stats"]