from ..config import Settings
from .base import Executor, RunFinished, RunNotFound, TooManyRuns, UnknownSuite


def make_executor(settings: Settings) -> Executor:
    if settings.executor == "local":
        from .local import LocalExecutor

        return LocalExecutor(settings)
    from .kubernetes import KubernetesExecutor

    return KubernetesExecutor(settings)


__all__ = ["Executor", "RunFinished", "RunNotFound", "TooManyRuns", "UnknownSuite", "make_executor"]
