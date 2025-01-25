import dataclasses
import os
import subprocess
import typing
from pathlib import Path
from typing import Iterable, Optional, TypeAlias, Union

import torch

DeviceType = Union[str, torch.device]


def iterable_from_file(path: Union[str, Path]) -> Iterable[str]:
    with open(path, 'r') as file:
        for line in file:
            yield line


def current_commit() -> str:
    """Get the last Git commit hash."""
    try:
        # Use '--short' for a shorter hash if needed
        args = ["git", "rev-parse", "HEAD"]

        # Run the Git command and decode the output
        hash = subprocess.check_output(args).strip().decode("utf-8")
        return hash
    except subprocess.CalledProcessError as e:
        print(f"Error retrieving commit hash: {e}")
        return "unknown-commit"


def compare_sequences(yhat: torch.Tensor, y: torch.Tensor) -> float:
    from grandpiano import GrandPiano
    if yhat.size(0) != y.size(0):
        padded = torch.full(
            (max(yhat.size(0), y.size(0)), GrandPiano.Stats.max_chord),
            fill_value=GrandPiano.PAD[0])
        if yhat.size(0) > y.size(0):
            padded[0:y.size(0), :] = y
            y = padded.to(y.device)
        else:
            padded[0:yhat.size(0), :] = yhat
            yhat = padded.to(yhat.device)
    wrong = torch.sum(y != yhat).item()
    total = torch.sum(y != GrandPiano.PAD[0]).item()
    return 1.0 - wrong / total


def path_substract(shorter: Path, longer: Path) -> Path:
    """Substract the shorter path from the longer to obtain a relative path.

        This function asserts that there is a common prefix to noth paths.

    Args:
        shorter (Path): The short path to remove.
        longer (Path): The longer path to remove shorter from.

    Returns:
        Path: _description_
    """
    prefix = os.path.commonprefix([shorter, longer])
    assert prefix is not None, f"Can't substract {shorter} from {longer}"
    return Path(os.path.relpath(longer, prefix))
