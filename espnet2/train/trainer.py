from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple

import numpy as np
import torch
import torch.nn
import torch.optim
from torch.nn.parallel import data_parallel
from torch.utils.data import DataLoader
from typeguard import check_argument_types
from typeguard import check_type

from espnet2.schedulers.abs_scheduler import AbsBatchStepScheduler
from espnet2.schedulers.abs_scheduler import AbsScheduler
from espnet2.torch_utils.add_gradient_noise import add_gradient_noise
from espnet2.torch_utils.calculate_all_attentions import calculate_all_attentions
from espnet2.torch_utils.device_funcs import to_device
from espnet2.train.abs_e2e import AbsE2E
from espnet2.train.reporter import SubReporter


@dataclasses.dataclass(frozen=True)
class TrainerOptions:
    ngpu: int
    train_dtype: str
    grad_noise: bool
    accum_grad: int
    grad_clip: float
    log_interval: Optional[int]
    no_forward_run: bool


def build_dataclass(dataclass, args: argparse.Namespace):
    """Helper function to build dataclass from 'args'."""
    kwargs = {}
    for field in dataclasses.fields(dataclass):
        if not hasattr(args, field.name):
            raise RuntimeError(
                f"args doesn't have {field.name}. You need to set it to ArgumentsParser"
            )
        check_type(field.name, getattr(args, field.name), field.type)
        kwargs[field.name] = getattr(args, field.name)
    return dataclass(**kwargs)


class Trainer:
    """Trainer having a optimizer.

    Trainer have a role to define the procedure for an epoch.
    >>> for epoch in range(max_epoch):
    ...     Trainer.train_one_epoch(...)
    ...     Trainer.eval_one_epoch(...)

    """

    # If you need more than one optimizers, change this value in inheritance
    num_optimizers: int = 1

    def __init__(self):
        raise RuntimeError("This class can't be instantiated.")

    @classmethod
    def build_options(cls, args: argparse.Namespace) -> TrainerOptions:
        """Build options consumed by train(), eval(), and plot_attention()"""
        assert check_argument_types()
        return build_dataclass(TrainerOptions, args)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        """Reserved for future development of another Trainer"""
        pass

    @classmethod
    def train_one_epoch(
        cls,
        model: AbsE2E,
        iterator: DataLoader and Iterable[Tuple[List[str], Dict[str, torch.Tensor]]],
        optimizers: Sequence[torch.optim.Optimizer],
        schedulers: Sequence[Optional[AbsScheduler]],
        reporter: SubReporter,
        options: TrainerOptions,
    ) -> bool:
        assert check_argument_types()

        # Note(kamo): assumes one optimizer
        assert cls.num_optimizers == 1, cls.num_optimizers
        assert len(optimizers) == 1, len(optimizers)
        optimizer = optimizers[0]
        scheduler = schedulers[0]

        grad_noise = options.grad_noise
        accum_grad = options.accum_grad
        grad_clip = options.grad_clip
        log_interval = options.log_interval
        no_forward_run = options.no_forward_run
        ngpu = options.ngpu
        train_dtype = options.train_dtype

        if log_interval is None:
            log_interval = max(len(iterator) // 20, 10)

        model.train()
        all_steps_are_invalid = True
        for iiter, (_, batch) in enumerate(iterator, 1):
            assert isinstance(batch, dict), type(batch)
            batch = to_device(batch, "cuda" if ngpu > 0 else "cpu")
            if no_forward_run:
                all_steps_are_invalid = False
                reporter.register({})
                continue

            if ngpu <= 1:
                # NOTE(kamo): data_parallel also should work with ngpu=1,
                # but for debuggability it's better to keep this block.
                loss, stats, weight = model(**batch)
            else:
                loss, stats, weight = data_parallel(
                    model, (), range(ngpu), module_kwargs=batch
                )
                # Weighted averaging of loss from torch-data-parallel
                loss = (loss * weight.to(loss.dtype)).sum(0) / weight.sum()
                stats = {
                    k: (v * weight.to(v.dtype)).sum(0) / weight.sum()
                    if v is not None
                    else None
                    for k, v in stats.items()
                }
                weight = weight.sum()
            reporter.register(stats, weight)

            if train_dtype in ("O0", "O1", "O2", "O3"):
                from apex import amp

                with amp.scale_loss(loss, optimizers) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # gradient noise injection
            if grad_noise:
                add_gradient_noise(
                    model,
                    reporter.get_total_count(),
                    duration=100,
                    eta=1.0,
                    scale_factor=0.55,
                )

            # compute the gradient norm to check if it is normal or not
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            if iiter % accum_grad == 0:
                if not np.isfinite(grad_norm):
                    logging.warning(
                        f"The grad norm is {grad_norm}. Skipping updating the model."
                    )
                else:
                    all_steps_are_invalid = False
                    optimizer.step()
                optimizer.zero_grad()
                if isinstance(scheduler, AbsBatchStepScheduler):
                    scheduler.step()

                # Register lr
                reporter.register(
                    {
                        f"lr_{i}": pg["lr"]
                        for i, pg in enumerate(optimizer.param_groups)
                        if "lr" in pg
                    },
                    not_increment_count=True,
                )

            if iiter % log_interval == 0:
                reporter.logging(nlatest=log_interval)
        return all_steps_are_invalid

    @classmethod
    @torch.no_grad()
    def eval_one_epoch(
        cls,
        model: AbsE2E,
        iterator: DataLoader and Iterable[Dict[str, torch.Tensor]],
        reporter: SubReporter,
        options: TrainerOptions,
    ) -> None:
        assert check_argument_types()
        ngpu = options.ngpu
        no_forward_run = options.no_forward_run

        model.eval()
        for (_, batch) in iterator:
            assert isinstance(batch, dict), type(batch)
            batch = to_device(batch, "cuda" if ngpu > 0 else "cpu")
            if no_forward_run:
                reporter.register({})
                continue

            if ngpu <= 1:
                _, stats, weight = model(**batch)
            else:
                _, stats, weight = data_parallel(
                    model, (), range(ngpu), module_kwargs=batch
                )
                stats = {
                    k: (v * weight.to(v.dtype)).sum(0) / weight.sum()
                    if v is not None
                    else None
                    for k, v in stats.items()
                }
                weight = weight.sum()

            reporter.register(stats, weight)

    @classmethod
    @torch.no_grad()
    def plot_attention(
        cls,
        model: AbsE2E,
        output_dir: Path,
        iterator: DataLoader and Iterable[Tuple[List[str], Dict[str, torch.Tensor]]],
        reporter: SubReporter,
        options: TrainerOptions,
    ) -> None:
        assert check_argument_types()
        import matplotlib

        ngpu = options.ngpu
        no_forward_run = options.no_forward_run

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator

        model.eval()
        output_dir = Path(output_dir)
        for ids, batch in iterator:
            assert isinstance(batch, dict), type(batch)
            assert len(next(iter(batch.values()))) == len(ids), (
                len(next(iter(batch.values()))),
                len(ids),
            )
            batch = to_device(batch, "cuda" if ngpu > 0 else "cpu")
            if no_forward_run:
                continue

            # 1. Forwarding model and gathering all attentions
            #    calculate_all_attentions() uses single gpu only.
            att_dict = calculate_all_attentions(model, batch)

            # 2. Plot attentions: This part is slow due to matplotlib
            for k, att_list in att_dict.items():
                assert len(att_list) == len(ids), (len(att_list), len(ids))
                for id_, att_w in zip(ids, att_list):

                    if isinstance(att_w, torch.Tensor):
                        att_w = att_w.detach().cpu().numpy()

                    if att_w.ndim == 2:
                        att_w = att_w[None]
                    elif att_w.ndim > 3 or att_w.ndim == 1:
                        raise RuntimeError(f"Must be 2 or 3 dimension: {att_w.ndim}")

                    w, h = plt.figaspect(1.0 / len(att_w))
                    fig = plt.Figure(figsize=(w * 1.3, h * 1.3))
                    axes = fig.subplots(1, len(att_w))
                    if len(att_w) == 1:
                        axes = [axes]

                    for ax, aw in zip(axes, att_w):
                        ax.imshow(aw.astype(np.float32), aspect="auto")
                        ax.set_title(f"{k}_{id_}")
                        ax.set_xlabel("Input")
                        ax.set_ylabel("Output")
                        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                        ax.yaxis.set_major_locator(MaxNLocator(integer=True))

                    p = output_dir / id_ / (k + ".png")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    fig.savefig(p)

                    # Dummy register() stimulates to increment the counter
                    reporter.register({})