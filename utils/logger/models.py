from dataclasses import dataclass

@dataclass
class WorkerData:
    key: str
    time: int
    data: str
    silent: bool