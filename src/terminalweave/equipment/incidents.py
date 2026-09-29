from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError

@dataclass
class EquipmentIncident:
    incident_id: str
    equipment_id: str
    at: datetime
    severity: int
    category: str
    lost_time_minutes: int = 0
    resolved_at: datetime | None = field(default=None)

    def __post_init__(self):
        self.at = ensure_utc(self.at)
        if not 1 <= self.severity <= 5 or self.lost_time_minutes < 0:
            raise ValidationError('invalid incident')
        if self.resolved_at is not None:
            self.resolved_at = ensure_utc(self.resolved_at)
            if self.resolved_at < self.at:
                raise ValidationError('resolution cannot precede incident')

    def resolve(self, at: datetime) -> None:
        at = ensure_utc(at)
        if self.resolved_at is not None:
            raise ValidationError('incident already resolved')
        self.resolved_at = at

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

class IncidentBook:

    def __init__(self):
        self.items: dict[str, EquipmentIncident] = {}

    def add(self, i: EquipmentIncident) -> None:
        if i.incident_id in self.items:
            raise ValidationError('duplicate incident')
        self.items[i.incident_id] = i

    def resolve(self, incident_id: str, at: datetime) -> EquipmentIncident:
        incident = self.items[incident_id]
        incident.resolve(at)
        return incident

    def active_for(self, equipment_id: str) -> tuple[EquipmentIncident, ...]:
        return tuple(i for i in self.items.values()
                     if i.equipment_id == equipment_id and not i.resolved)

    def frequency(self, equipment_id: str) -> int:
        return sum((i.equipment_id == equipment_id for i in self.items.values()))

    def lost_time(self, equipment_id: str | None=None) -> int:
        return sum((i.lost_time_minutes for i in self.items.values() if equipment_id is None or i.equipment_id == equipment_id))

    def severe(self, threshold: int=4) -> tuple[EquipmentIncident, ...]:
        return tuple(sorted((i for i in self.items.values() if i.severity >= threshold), key=lambda x: (-x.severity, x.at)))
