from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, List, TYPE_CHECKING

from gen.flights.loadouts import Loadout

from .flightroster import FlightRoster

if TYPE_CHECKING:
    from game.dcs.aircrafttype import AircraftType
    from game.squadrons import Squadron, Pilot
    from game.theater import ControlPoint, MissionTarget
    from game.transfers import TransferOrder
    from .flighttype import FlightType
    from .flightwaypoint import FlightWaypoint
    from .package import Package
    from .starttype import StartType


class Flight:
    def __init__(
        self,
        package: Package,
        country: str,
        squadron: Squadron,
        count: int,
        flight_type: FlightType,
        start_type: StartType,
        divert: Optional[ControlPoint],
        custom_name: Optional[str] = None,
        cargo: Optional[TransferOrder] = None,
        roster: Optional[FlightRoster] = None,
    ) -> None:
        self.package = package
        self.country = country
        self.squadron = squadron
        self.squadron.claim_inventory(count)
        if roster is None:
            self.roster = FlightRoster(self.squadron, initial_size=count)
        else:
            self.roster = roster
        self.divert = divert
        self.flight_type = flight_type
        # TODO: Replace with FlightPlan.
        self.targets: List[MissionTarget] = []
        self.loadout = Loadout.default_for(self)
        self.start_type = start_type
        self.use_custom_loadout = False
        self.custom_name = custom_name

        # Only used by transport missions.
        self.cargo = cargo

        # Will be replaced with a more appropriate FlightPlan by
        # FlightPlanBuilder, but an empty flight plan the flight begins with an
        # empty flight plan.
        from gen.flights.flightplan import FlightPlan, CustomFlightPlan

        self.flight_plan: FlightPlan = CustomFlightPlan(
            package=package, flight=self, custom_waypoints=[]
        )

    @property
    def departure(self) -> ControlPoint:
        return self.squadron.location

    @property
    def arrival(self) -> ControlPoint:
        return self.squadron.arrival

    @property
    def count(self) -> int:
        return self.roster.max_size

    @property
    def client_count(self) -> int:
        return self.roster.player_count

    @property
    def unit_type(self) -> AircraftType:
        return self.squadron.aircraft

    @property
    def from_cp(self) -> ControlPoint:
        return self.departure

    @property
    def points(self) -> List[FlightWaypoint]:
        return self.flight_plan.waypoints[1:]

    def resize(self, new_size: int) -> None:
        self.squadron.claim_inventory(new_size - self.count)
        self.roster.resize(new_size)

    def set_pilot(self, index: int, pilot: Optional[Pilot]) -> None:
        self.roster.set_pilot(index, pilot)

    @property
    def missing_pilots(self) -> int:
        return self.roster.missing_pilots

    def return_pilots_and_aircraft(self) -> None:
        self.roster.clear()
        self.squadron.claim_inventory(-self.count)

    def __repr__(self) -> str:
        if self.custom_name:
            return f"{self.custom_name} {self.count} x {self.unit_type}"
        return f"[{self.flight_type}] {self.count} x {self.unit_type}"

    def __str__(self) -> str:
        if self.custom_name:
            return f"{self.custom_name} {self.count} x {self.unit_type}"
        return f"[{self.flight_type}] {self.count} x {self.unit_type}"