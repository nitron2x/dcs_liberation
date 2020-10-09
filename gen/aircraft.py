from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Type, Union

from dcs import helicopters
from dcs.action import AITaskPush, ActivateGroup, MessageToAll
from dcs.condition import CoalitionHasAirdrome, PartOfCoalitionInZone, TimeAfter
from dcs.country import Country
from dcs.flyingunit import FlyingUnit
from dcs.helicopters import UH_1H, helicopter_map
from dcs.mapping import Point
from dcs.mission import Mission, StartType
from dcs.planes import (
    AJS37,
    B_17G,
    Bf_109K_4,
    FW_190A8,
    FW_190D9,
    F_14B,
    I_16,
    JF_17,
    Ju_88A4,
    P_47D_30,
    P_51D,
    P_51D_30_NA,
    SpitfireLFMkIX,
    SpitfireLFMkIXCW,
    Su_33,
)
from dcs.point import MovingPoint, PointAction
from dcs.task import (
    AntishipStrike,
    AttackGroup,
    Bombing,
    CAP,
    CAS,
    ControlledTask,
    EPLRS,
    EngageTargets,
    Escort,
    GroundAttack,
    OptROE,
    OptRTBOnBingoFuel,
    OptRTBOnOutOfAmmo,
    OptReactOnThreat,
    OptRestrictAfterburner,
    OptRestrictJettison,
    OrbitAction,
    PinpointStrike,
    SEAD,
    StartCommand,
    Targets,
    Task,
)
from dcs.terrain.terrain import Airport, NoParkingSlotError
from dcs.translation import String
from dcs.triggers import Event, TriggerOnce
from dcs.unitgroup import FlyingGroup, Group, ShipGroup, StaticGroup
from dcs.unittype import FlyingType, UnitType

from game import db
from game.data.cap_capabilities_db import GUNFIGHTERS
from game.settings import Settings
from game.utils import meter_to_nm, nm_to_meter
from gen.airfields import RunwayData
from gen.airsupportgen import AirSupport
from gen.ato import AirTaskingOrder, Package
from gen.callsigns import create_group_callsign_from_unit
from gen.flights.flight import (
    Flight,
    FlightType,
    FlightWaypoint,
    FlightWaypointType,
)
from gen.radios import MHz, Radio, RadioFrequency, RadioRegistry, get_radio
from theater import MissionTarget, TheaterGroundObject
from theater.controlpoint import ControlPoint, ControlPointType
from .conflictgen import Conflict
from .naming import namegen

WARM_START_HELI_AIRSPEED = 120
WARM_START_HELI_ALT = 500
WARM_START_ALTITUDE = 3000
WARM_START_AIRSPEED = 550

CAP_DURATION = 30 # minutes

RTB_ALTITUDE = 800
RTB_DISTANCE = 5000
HELI_ALT = 500

# Note that fallback radio channels will *not* be reserved. It's possible that
# flights using these will overlap with other channels. This is because we would
# need to make sure we fell back to a frequency that is not used by any beacon
# or ATC, which we don't have the information to predict. Deal with the minor
# annoyance for now since we'll be fleshing out radio info soon enough.
ALLIES_WW2_CHANNEL = MHz(124)
GERMAN_WW2_CHANNEL = MHz(40)
HELICOPTER_CHANNEL = MHz(127)
UHF_FALLBACK_CHANNEL = MHz(251)


# TODO: Get radio information for all the special cases.
def get_fallback_channel(unit_type: UnitType) -> RadioFrequency:
    if unit_type in helicopter_map.values() and unit_type != UH_1H:
        return HELICOPTER_CHANNEL

    german_ww2_aircraft = [
        Bf_109K_4,
        FW_190A8,
        FW_190D9,
        Ju_88A4,
    ]

    if unit_type in german_ww2_aircraft:
        return GERMAN_WW2_CHANNEL

    allied_ww2_aircraft = [
        I_16,
        P_47D_30,
        P_51D,
        P_51D_30_NA,
        SpitfireLFMkIX,
        SpitfireLFMkIXCW,
    ]

    if unit_type in allied_ww2_aircraft:
        return ALLIES_WW2_CHANNEL

    return UHF_FALLBACK_CHANNEL


class ChannelNamer:
    """Base class allowing channel name customization per-aircraft.

    Most aircraft will want to customize this behavior, but the default is
    reasonable for any aircraft with numbered radios.
    """

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        """Returns the name of the channel for the given radio and channel."""
        return f"COMM{radio_id} Ch {channel_id}"


class MirageChannelNamer(ChannelNamer):
    """Channel namer for the M-2000."""

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        radio_name = ["V/UHF", "UHF"][radio_id - 1]
        return f"{radio_name} Ch {channel_id}"


class TomcatChannelNamer(ChannelNamer):
    """Channel namer for the F-14."""

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        radio_name = ["UHF", "VHF/UHF"][radio_id - 1]
        return f"{radio_name} Ch {channel_id}"


class ViggenChannelNamer(ChannelNamer):
    """Channel namer for the AJS37."""

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        if channel_id >= 4:
            channel_letter = "EFGH"[channel_id - 4]
            return f"FR 24 {channel_letter}"
        return f"FR 22 Special {channel_id}"


class ViperChannelNamer(ChannelNamer):
    """Channel namer for the F-16."""

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        return f"COM{radio_id} Ch {channel_id}"


class SCR522ChannelNamer(ChannelNamer):
    """
    Channel namer for P-51 & P-47D
    """

    @staticmethod
    def channel_name(radio_id: int, channel_id: int) -> str:
        if channel_id > 3:
            return "?"
        else:
            return f"Button " + "ABCD"[channel_id - 1]


@dataclass(frozen=True)
class ChannelAssignment:
    radio_id: int
    channel: int


@dataclass
class FlightData:
    """Details of a planned flight."""

    flight_type: FlightType

    #: All units in the flight.
    units: List[FlyingUnit]

    #: Total number of aircraft in the flight.
    size: int

    #: True if this flight belongs to the player's coalition.
    friendly: bool

    #: Number of minutes after mission start the flight is set to depart.
    departure_delay: int

    #: Arrival airport.
    arrival: RunwayData

    #: Departure airport.
    departure: RunwayData

    #: Diver airport.
    divert: Optional[RunwayData]

    #: Waypoints of the flight plan.
    waypoints: List[FlightWaypoint]

    #: Radio frequency for intra-flight communications.
    intra_flight_channel: RadioFrequency

    #: Map of radio frequencies to their assigned radio and channel, if any.
    frequency_to_channel_map: Dict[RadioFrequency, ChannelAssignment]

    #: Data concerning the target of a CAS/Strike/SEAD flight, or None else
    targetPoint = None

    def __init__(self, flight_type: FlightType, units: List[FlyingUnit],
                 size: int, friendly: bool, departure_delay: int,
                 departure: RunwayData, arrival: RunwayData,
                 divert: Optional[RunwayData], waypoints: List[FlightWaypoint],
                 intra_flight_channel: RadioFrequency, targetPoint: Optional) -> None:
        self.flight_type = flight_type
        self.units = units
        self.size = size
        self.friendly = friendly
        self.departure_delay = departure_delay
        self.departure = departure
        self.arrival = arrival
        self.divert = divert
        self.waypoints = waypoints
        self.intra_flight_channel = intra_flight_channel
        self.frequency_to_channel_map = {}
        self.callsign = create_group_callsign_from_unit(self.units[0])
        self.targetPoint = targetPoint

    @property
    def client_units(self) -> List[FlyingUnit]:
        """List of playable units in the flight."""
        return [u for u in self.units if u.is_human()]

    @property
    def aircraft_type(self) -> FlyingType:
        """Returns the type of aircraft in this flight."""
        return self.units[0].unit_type

    def num_radio_channels(self, radio_id: int) -> int:
        """Returns the number of preset channels for the given radio."""
        # Note: pydcs only initializes the radio presets for client slots.
        return self.client_units[0].num_radio_channels(radio_id)

    def channel_for(
            self, frequency: RadioFrequency) -> Optional[ChannelAssignment]:
        """Returns the radio and channel number for the given frequency."""
        return self.frequency_to_channel_map.get(frequency, None)

    def assign_channel(self, radio_id: int, channel_id: int,
                       frequency: RadioFrequency) -> None:
        """Assigns a preset radio channel to the given frequency."""
        for unit in self.client_units:
            unit.set_radio_channel_preset(radio_id, channel_id, frequency.mhz)

        # One frequency could be bound to multiple channels. Prefer the first,
        # since with the current implementation it will be the lowest numbered
        # channel.
        if frequency not in self.frequency_to_channel_map:
            self.frequency_to_channel_map[frequency] = ChannelAssignment(
                radio_id, channel_id
            )


class RadioChannelAllocator:
    """Base class for radio channel allocators."""

    def assign_channels_for_flight(self, flight: FlightData,
                                   air_support: AirSupport) -> None:
        """Assigns mission frequencies to preset channels for the flight."""
        raise NotImplementedError


@dataclass(frozen=True)
class CommonRadioChannelAllocator(RadioChannelAllocator):
    """Radio channel allocator suitable for most aircraft.

    Most of the aircraft with preset channels available have one or more radios
    with 20 or more channels available (typically per-radio, but this is not the
    case for the JF-17).
    """

    #: Index of the radio used for intra-flight communications. Matches the
    #: index of the panel_radio field of the pydcs.dcs.planes object.
    inter_flight_radio_index: Optional[int]

    #: Index of the radio used for intra-flight communications. Matches the
    #: index of the panel_radio field of the pydcs.dcs.planes object.
    intra_flight_radio_index: Optional[int]

    def assign_channels_for_flight(self, flight: FlightData,
                                   air_support: AirSupport) -> None:
        if self.intra_flight_radio_index is not None:
            flight.assign_channel(
                self.intra_flight_radio_index, 1, flight.intra_flight_channel)

        if self.inter_flight_radio_index is None:
            return

        # For cases where the inter-flight and intra-flight radios share presets
        # (the JF-17 only has one set of channels, even though it can use two
        # channels simultaneously), start assigning inter-flight channels at 2.
        radio_id = self.inter_flight_radio_index
        if self.intra_flight_radio_index == radio_id:
            first_channel = 2
        else:
            first_channel = 1

        last_channel = flight.num_radio_channels(radio_id)
        channel_alloc = iter(range(first_channel, last_channel + 1))

        if flight.departure.atc is not None:
            flight.assign_channel(radio_id, next(channel_alloc),
                                  flight.departure.atc)

        # TODO: If there ever are multiple AWACS, limit to mission relevant.
        for awacs in air_support.awacs:
            flight.assign_channel(radio_id, next(channel_alloc), awacs.freq)

        if flight.arrival != flight.departure and flight.arrival.atc is not None:
            flight.assign_channel(radio_id, next(channel_alloc),
                                  flight.arrival.atc)

        try:
            # TODO: Skip incompatible tankers.
            for tanker in air_support.tankers:
                flight.assign_channel(
                    radio_id, next(channel_alloc), tanker.freq)

            if flight.divert is not None and flight.divert.atc is not None:
                flight.assign_channel(radio_id, next(channel_alloc),
                                      flight.divert.atc)
        except StopIteration:
            # Any remaining channels are nice-to-haves, but not necessary for
            # the few aircraft with a small number of channels available.
            pass


@dataclass(frozen=True)
class WarthogRadioChannelAllocator(RadioChannelAllocator):
    """Preset channel allocator for the A-10C."""

    def assign_channels_for_flight(self, flight: FlightData,
                                   air_support: AirSupport) -> None:
        # The A-10's radio works differently than most aircraft. Doesn't seem to
        # be a way to set these from the mission editor, let alone pydcs.
        pass


@dataclass(frozen=True)
class ViggenRadioChannelAllocator(RadioChannelAllocator):
    """Preset channel allocator for the AJS37."""

    def assign_channels_for_flight(self, flight: FlightData,
                                   air_support: AirSupport) -> None:
        # The Viggen's preset channels are handled differently from other
        # aircraft. The aircraft automatically configures channels for every
        # allied flight in the game (including AWACS) and for every airfield. As
        # such, we don't need to allocate any of those. There are seven presets
        # we can modify, however: three channels for the main radio intended for
        # communication with wingmen, and four emergency channels for the backup
        # radio. We'll set the first channel of the main radio to the
        # intra-flight channel, and the first three emergency channels to each
        # of the flight plan's airfields. The fourth emergency channel is always
        # the guard channel.
        radio_id = 1
        flight.assign_channel(radio_id, 1, flight.intra_flight_channel)
        if flight.departure.atc is not None:
            flight.assign_channel(radio_id, 4, flight.departure.atc)
        if flight.arrival.atc is not None:
            flight.assign_channel(radio_id, 5, flight.arrival.atc)
        # TODO: Assign divert to 6 when we support divert airfields.


@dataclass(frozen=True)
class SCR522RadioChannelAllocator(RadioChannelAllocator):
    """Preset channel allocator for the SCR522 WW2 radios. (4 channels)"""

    def assign_channels_for_flight(self, flight: FlightData,
                                   air_support: AirSupport) -> None:
        radio_id = 1
        flight.assign_channel(radio_id, 1, flight.intra_flight_channel)
        if flight.departure.atc is not None:
            flight.assign_channel(radio_id, 2, flight.departure.atc)
        if flight.arrival.atc is not None:
            flight.assign_channel(radio_id, 3, flight.arrival.atc)

        # TODO : Some GCI on Channel 4 ?

@dataclass(frozen=True)
class AircraftData:
    """Additional aircraft data not exposed by pydcs."""

    #: The type of radio used for inter-flight communications.
    inter_flight_radio: Radio

    #: The type of radio used for intra-flight communications.
    intra_flight_radio: Radio

    #: The radio preset channel allocator, if the aircraft supports channel
    #: presets. If the aircraft does not support preset channels, this will be
    #: None.
    channel_allocator: Optional[RadioChannelAllocator]

    #: Defines how channels should be named when printed in the kneeboard.
    channel_namer: Type[ChannelNamer] = ChannelNamer


# Indexed by the id field of the pydcs PlaneType.
AIRCRAFT_DATA: Dict[str, AircraftData] = {
    "A-10C": AircraftData(
        inter_flight_radio=get_radio("AN/ARC-164"),
        intra_flight_radio=get_radio("AN/ARC-164"), # VHF for intraflight is not accepted anymore by DCS (see https://forums.eagle.ru/showthread.php?p=4499738)
        channel_allocator=WarthogRadioChannelAllocator()
    ),

    "AJS37": AircraftData(
        # The AJS37 has somewhat unique radio configuration. Two backup radio
        # (FR 24) can only operate simultaneously with the main radio in guard
        # mode. As such, we only use the main radio for both inter- and intra-
        # flight communication.
        inter_flight_radio=get_radio("FR 22"),
        intra_flight_radio=get_radio("FR 22"),
        channel_allocator=ViggenRadioChannelAllocator(),
        channel_namer=ViggenChannelNamer
    ),

    "AV8BNA": AircraftData(
        inter_flight_radio=get_radio("AN/ARC-210"),
        intra_flight_radio=get_radio("AN/ARC-210"),
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=2,
            intra_flight_radio_index=1
        )
    ),

    "F-14B": AircraftData(
        inter_flight_radio=get_radio("AN/ARC-159"),
        intra_flight_radio=get_radio("AN/ARC-182"),
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=1,
            intra_flight_radio_index=2
        ),
        channel_namer=TomcatChannelNamer
    ),

    "F-16C_50": AircraftData(
        inter_flight_radio=get_radio("AN/ARC-164"),
        intra_flight_radio=get_radio("AN/ARC-222"),
        # COM2 is the AN/ARC-222, which is the VHF radio we want to use for
        # intra-flight communication to leave COM1 open for UHF inter-flight.
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=1,
            intra_flight_radio_index=2
        ),
        channel_namer=ViperChannelNamer
    ),

    "FA-18C_hornet": AircraftData(
        inter_flight_radio=get_radio("AN/ARC-210"),
        intra_flight_radio=get_radio("AN/ARC-210"),
        # DCS will clobber channel 1 of the first radio compatible with the
        # flight's assigned frequency. Since the F/A-18's two radios are both
        # AN/ARC-210s, radio 1 will be compatible regardless of which frequency
        # is assigned, so we must use radio 1 for the intra-flight radio.
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=2,
            intra_flight_radio_index=1
        )
    ),

    "JF-17": AircraftData(
        inter_flight_radio=get_radio("R&S M3AR UHF"),
        intra_flight_radio=get_radio("R&S M3AR VHF"),
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=1,
            intra_flight_radio_index=1
        ),
        # Same naming pattern as the Viper, so just reuse that.
        channel_namer=ViperChannelNamer
    ),

    "M-2000C": AircraftData(
        inter_flight_radio=get_radio("TRT ERA 7000 V/UHF"),
        intra_flight_radio=get_radio("TRT ERA 7200 UHF"),
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=1,
            intra_flight_radio_index=2
        ),
        channel_namer=MirageChannelNamer
    ),

    "P-51D": AircraftData(
        inter_flight_radio=get_radio("SCR522"),
        intra_flight_radio=get_radio("SCR522"),
        channel_allocator=CommonRadioChannelAllocator(
            inter_flight_radio_index=1,
            intra_flight_radio_index=1
        ),
        channel_namer=SCR522ChannelNamer
    ),
}
AIRCRAFT_DATA["P-51D-30-NA"] = AIRCRAFT_DATA["P-51D"]
AIRCRAFT_DATA["P-47D-30"] = AIRCRAFT_DATA["P-51D"]


@dataclass(frozen=True)
class PackageWaypointTiming:
    #: The package being scheduled.
    package: Package

    #: The package join time.
    join: int

    #: The ingress waypoint TOT.
    ingress: int

    #: The egress waypoint TOT.
    egress: int

    #: The package split time.
    split: int

    @property
    def target(self) -> int:
        """The package time over target."""
        assert self.package.time_over_target is not None
        return self.package.time_over_target

    @property
    def race_track_start(self) -> Optional[int]:
        cap_types = (FlightType.BARCAP, FlightType.CAP)
        if self.package.primary_task in cap_types:
            # CAP flights don't have hold points, and we don't calculate takeoff
            # times yet or adjust the TOT based on when the flight can arrive,
            # so if we set a TOT that gives the flight a lot of extra time it
            # will just fly to the start point slowly, possibly slowly enough to
            # stall and crash. Just don't set a TOT for these points and let the
            # CAP get on station ASAP.
            return None
        else:
            return self.ingress

    @property
    def race_track_end(self) -> int:
        cap_types = (FlightType.BARCAP, FlightType.CAP)
        if self.package.primary_task in cap_types:
            return self.target + CAP_DURATION * 60
        else:
            return self.egress

    def push_time(self, flight: Flight, hold_point: Point) -> int:
        assert self.package.waypoints is not None
        return self.join - self.travel_time(
            hold_point,
            self.package.waypoints.join,
            self.flight_ground_speed(flight)
        )

    def tot_for_waypoint(self, waypoint: FlightWaypoint) -> Optional[int]:
        target_types = (
            FlightWaypointType.TARGET_GROUP_LOC,
            FlightWaypointType.TARGET_POINT,
            FlightWaypointType.TARGET_SHIP,
        )

        ingress_types = (
            FlightWaypointType.INGRESS_CAS,
            FlightWaypointType.INGRESS_SEAD,
            FlightWaypointType.INGRESS_STRIKE,
        )

        if waypoint.waypoint_type == FlightWaypointType.JOIN:
            return self.join
        elif waypoint.waypoint_type in ingress_types:
            return self.ingress
        elif waypoint.waypoint_type in target_types:
            return self.target
        elif waypoint.waypoint_type == FlightWaypointType.EGRESS:
            return self.egress
        elif waypoint.waypoint_type == FlightWaypointType.SPLIT:
            return self.split
        elif waypoint.waypoint_type == FlightWaypointType.PATROL_TRACK:
            return self.race_track_start
        return None

    def depart_time_for_waypoint(self, waypoint: FlightWaypoint,
                                 flight: Flight) -> Optional[int]:
        if waypoint.waypoint_type == FlightWaypointType.LOITER:
            return self.push_time(flight, Point(waypoint.x, waypoint.y))
        elif waypoint.waypoint_type == FlightWaypointType.PATROL:
            return self.race_track_end
        return None

    @classmethod
    def for_package(cls, package: Package) -> PackageWaypointTiming:
        assert package.time_over_target is not None
        assert package.waypoints is not None

        group_ground_speed = cls.package_ground_speed(package)

        ingress = package.time_over_target - cls.travel_time(
            package.waypoints.ingress,
            package.target.position,
            group_ground_speed
        )

        join = ingress - cls.travel_time(
            package.waypoints.join,
            package.waypoints.ingress,
            group_ground_speed
        )

        egress = package.time_over_target + cls.travel_time(
            package.target.position,
            package.waypoints.egress,
            group_ground_speed
        )

        split = egress + cls.travel_time(
            package.waypoints.egress,
            package.waypoints.split,
            group_ground_speed
        )

        return cls(package, join, ingress, egress, split)

    @classmethod
    def package_ground_speed(cls, package: Package) -> int:
        speeds = []
        for flight in package.flights:
            speeds.append(cls.flight_ground_speed(flight))
        return min(speeds)  # knots

    @staticmethod
    def flight_ground_speed(_flight: Flight) -> int:
        # TODO: Gather data so this is useful.
        return 400  # knots

    @staticmethod
    def travel_time(a: Point, b: Point, speed: float) -> int:
        error_factor = 1.1
        distance = meter_to_nm(a.distance_to_point(b))
        hours = distance / speed
        seconds = hours * 3600
        return int(seconds * error_factor)


class AircraftConflictGenerator:
    def __init__(self, mission: Mission, conflict: Conflict, settings: Settings,
                 game, radio_registry: RadioRegistry):
        self.m = mission
        self.game = game
        self.settings = settings
        self.conflict = conflict
        self.radio_registry = radio_registry
        self.escort_targets: List[Tuple[FlyingGroup, int]] = []
        self.flights: List[FlightData] = []

    def get_intra_flight_channel(self, airframe: UnitType) -> RadioFrequency:
        """Allocates an intra-flight channel to a group.

        Args:
            airframe: The type of aircraft a channel should be allocated for.

        Returns:
            The frequency of the intra-flight channel.
        """
        try:
            aircraft_data = AIRCRAFT_DATA[airframe.id]
            return self.radio_registry.alloc_for_radio(
                aircraft_data.intra_flight_radio)
        except KeyError:
            return get_fallback_channel(airframe)

    def _start_type(self) -> StartType:
        return self.settings.cold_start and StartType.Cold or StartType.Warm

    def _setup_group(self, group: FlyingGroup, for_task: Type[Task],
                     flight: Flight, dynamic_runways: Dict[str, RunwayData]):
        did_load_loadout = False
        unit_type = group.units[0].unit_type

        if unit_type in db.PLANE_PAYLOAD_OVERRIDES:
            override_loadout = db.PLANE_PAYLOAD_OVERRIDES[unit_type]
            # Clear pylons
            for p in group.units:
                p.pylons.clear()

            # Now load loadout
            if for_task in db.PLANE_PAYLOAD_OVERRIDES[unit_type]:
                payload_name = db.PLANE_PAYLOAD_OVERRIDES[unit_type][for_task]
                group.load_loadout(payload_name)
                did_load_loadout = True
                logging.info("Loaded overridden payload for {} - {} for task {}".format(unit_type, payload_name, for_task))

        if not did_load_loadout:
            group.load_task_default_loadout(for_task)

        if unit_type in db.PLANE_LIVERY_OVERRIDES:
            for unit_instance in group.units:
                unit_instance.livery_id = db.PLANE_LIVERY_OVERRIDES[unit_type]

        single_client = flight.client_count == 1
        for idx in range(0, min(len(group.units), flight.client_count)):
            unit = group.units[idx]
            if single_client:
                unit.set_player()
            else:
                unit.set_client()

            # Do not generate player group with late activation.
            if group.late_activation:
                group.late_activation = False

            # Set up F-14 Client to have pre-stored alignement
            if unit_type is F_14B:
                unit.set_property(F_14B.Properties.INSAlignmentStored.id, True)


        group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))

        channel = self.get_intra_flight_channel(unit_type)
        group.set_frequency(channel.mhz)

        # TODO: Support for different departure/arrival airfields.
        cp = flight.from_cp
        fallback_runway = RunwayData(cp.full_name, runway_name="")
        if cp.cptype == ControlPointType.AIRBASE:
            departure_runway = self.get_preferred_runway(flight.from_cp.airport)
        elif cp.is_fleet:
            departure_runway = dynamic_runways.get(cp.name, fallback_runway)
        else:
            logging.warning(f"Unhandled departure control point: {cp.cptype}")
            departure_runway = fallback_runway

        # The first waypoint is set automatically by pydcs, so it's not in our
        # list. Convert the pydcs MovingPoint to a FlightWaypoint so it shows up
        # in our FlightData.
        first_point = FlightWaypoint.from_pydcs(group.points[0], flight.from_cp)
        self.flights.append(FlightData(
            flight_type=flight.flight_type,
            units=group.units,
            size=len(group.units),
            friendly=flight.from_cp.captured,
            departure_delay=flight.scheduled_in,
            departure=departure_runway,
            arrival=departure_runway,
            # TODO: Support for divert airfields.
            divert=None,
            # Waypoints are added later, after they've had their TOTs set.
            waypoints=[],
            intra_flight_channel=channel,
            targetPoint=flight.targetPoint,
        ))

        # Special case so Su 33 carrier take off
        if unit_type is Su_33:
            if flight.flight_type is not CAP:
                for unit in group.units:
                    unit.fuel = Su_33.fuel_max / 2.2
            else:
                for unit in group.units:
                    unit.fuel = Su_33.fuel_max * 0.8

    def get_preferred_runway(self, airport: Airport) -> RunwayData:
        """Returns the preferred runway for the given airport.

        Right now we're only selecting runways based on whether or not they have
        ILS, but we could also choose based on wind conditions, or which
        direction flight plans should follow.
        """
        runways = list(RunwayData.for_pydcs_airport(airport))
        for runway in runways:
            # Prefer any runway with ILS.
            if runway.ils is not None:
                return runway
        # Otherwise we lack the mission information to pick more usefully,
        # so just use the first runway.
        return runways[0]

    def _generate_at_airport(self, name: str, side: Country,
                             unit_type: FlyingType, count: int,
                             client_count: int,
                             airport: Optional[Airport] = None,
                             start_type=None) -> FlyingGroup:
        assert count > 0

        if start_type is None:
            start_type = self._start_type()

        logging.info("airgen: {} for {} at {}".format(unit_type, side.id, airport))
        return self.m.flight_group_from_airport(
            country=side,
            name=name,
            aircraft_type=unit_type,
            airport=airport,
            maintask=None,
            start_type=start_type,
            group_size=count,
            parking_slots=None)

    def _generate_inflight(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, at: Point) -> FlyingGroup:
        assert count > 0

        if unit_type in helicopters.helicopter_map.values():
            alt = WARM_START_HELI_ALT
            speed = WARM_START_HELI_AIRSPEED
        else:
            alt = WARM_START_ALTITUDE
            speed = WARM_START_AIRSPEED

        pos = Point(at.x + random.randint(100, 1000), at.y + random.randint(100, 1000))

        logging.info("airgen: {} for {} at {} at {}".format(unit_type, side.id, alt, speed))
        group = self.m.flight_group(
            country=side,
            name=name,
            aircraft_type=unit_type,
            airport=None,
            position=pos,
            altitude=alt,
            speed=speed,
            maintask=None,
            start_type=self._start_type(),
            group_size=count)

        group.points[0].alt_type = "RADIO"
        return group

    def _generate_at_group(self, name: str, side: Country,
                           unit_type: FlyingType, count: int, client_count: int,
                           at: Union[ShipGroup, StaticGroup],
                           start_type=None) -> FlyingGroup:
        assert count > 0

        if start_type is None:
            start_type = self._start_type()

        logging.info("airgen: {} for {} at unit {}".format(unit_type, side.id, at))
        return self.m.flight_group_from_unit(
            country=side,
            name=name,
            aircraft_type=unit_type,
            pad_group=at,
            maintask=None,
            start_type=start_type,
            group_size=count)

    def _generate_group(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, at: db.StartingPosition):
        if isinstance(at, Point):
            return self._generate_inflight(name, side, unit_type, count, client_count, at)
        elif isinstance(at, Group):
            takeoff_ban = unit_type in db.CARRIER_TAKEOFF_BAN
            ai_ban = client_count == 0 and self.settings.only_player_takeoff

            if not takeoff_ban and not ai_ban:
                return self._generate_at_group(name, side, unit_type, count, client_count, at)
            else:
                return self._generate_inflight(name, side, unit_type, count, client_count, at.position)
        elif isinstance(at, Airport):
            takeoff_ban = unit_type in db.TAKEOFF_BAN
            ai_ban = client_count == 0 and self.settings.only_player_takeoff

            if not takeoff_ban and not ai_ban:
                try:
                    return self._generate_at_airport(name, side, unit_type, count, client_count, at)
                except NoParkingSlotError:
                    logging.info("No parking slot found at " + at.name + ", switching to air start.")
                    pass
            return self._generate_inflight(name, side, unit_type, count, client_count, at.position)
        else:
            assert False

    def _add_radio_waypoint(self, group: FlyingGroup, position, altitude: int, airspeed: int = 600):
        point = group.add_waypoint(position, altitude, airspeed)
        point.alt_type = "RADIO"
        return point

    def _rtb_for(self, group: FlyingGroup, cp: ControlPoint,
                 at: Optional[db.StartingPosition] = None):
        if at is None:
            at = cp.at
        position = at if isinstance(at, Point) else at.position

        last_waypoint = group.points[-1]
        if last_waypoint is not None:
            heading = position.heading_between_point(last_waypoint.position)
            tod_location = position.point_from_heading(heading, RTB_DISTANCE)
            self._add_radio_waypoint(group, tod_location, last_waypoint.alt)

        destination_waypoint = self._add_radio_waypoint(group, position, RTB_ALTITUDE)
        if isinstance(at, Airport):
            group.land_at(at)
        return destination_waypoint

    def _at_position(self, at) -> Point:
        if isinstance(at, Point):
            return at
        elif isinstance(at, ShipGroup):
            return at.position
        elif issubclass(at, Airport):
            return at.position
        else:
            assert False


    def _setup_custom_payload(self, flight, group:FlyingGroup):
        if flight.use_custom_loadout:

            logging.info("Custom loadout for flight : " + flight.__repr__())
            for p in group.units:
                p.pylons.clear()

            for key in flight.loadout.keys():
                if "Pylon" + key in flight.unit_type.__dict__.keys():
                    print(flight.loadout)
                    weapon_dict = flight.unit_type.__dict__["Pylon" + key].__dict__
                    if flight.loadout[key] in weapon_dict.keys():
                        weapon = weapon_dict[flight.loadout[key]]
                        group.load_pylon(weapon, int(key))
                else:
                    logging.warning("Pylon not found ! => Pylon" + key + " on " + str(flight.unit_type))

    def clear_parking_slots(self) -> None:
        for cp in self.game.theater.controlpoints:
            if cp.airport is not None:
                for parking_slot in cp.airport.parking_slots:
                    parking_slot.unit_id = None

    def generate_flights(self, country, ato: AirTaskingOrder,
                         dynamic_runways: Dict[str, RunwayData]) -> None:
        self.clear_parking_slots()

        for package in ato.packages:
            timing = PackageWaypointTiming.for_package(package)
            for flight in package.flights:
                culled = self.game.position_culled(flight.from_cp.position)
                if flight.client_count == 0 and culled:
                    logging.info("Flight not generated: culled")
                    continue
                logging.info(f"Generating flight: {flight.unit_type}")
                group = self.generate_planned_flight(flight.from_cp, country,
                                                     flight)
                self.setup_flight_group(group, flight, timing, dynamic_runways)
                self.setup_group_activation_trigger(flight, group)

    def setup_group_activation_trigger(self, flight, group):
        if flight.scheduled_in > 0 and flight.client_count == 0:

            if flight.start_type != "In Flight" and flight.from_cp.cptype not in [ControlPointType.AIRCRAFT_CARRIER_GROUP, ControlPointType.LHA_GROUP]:
                group.late_activation = False
                group.uncontrolled = True

                activation_trigger = TriggerOnce(Event.NoEvent, "FlightStartTrigger" + str(group.id))
                activation_trigger.add_condition(TimeAfter(seconds=flight.scheduled_in * 60))
                if (flight.from_cp.cptype == ControlPointType.AIRBASE):
                    if flight.from_cp.captured:
                        activation_trigger.add_condition(
                            CoalitionHasAirdrome(self.game.get_player_coalition_id(), flight.from_cp.id))
                    else:
                        activation_trigger.add_condition(
                            CoalitionHasAirdrome(self.game.get_enemy_coalition_id(), flight.from_cp.id))

                if flight.flight_type == FlightType.INTERCEPTION:
                    self.setup_interceptor_triggers(group, flight, activation_trigger)

                group.add_trigger_action(StartCommand())
                activation_trigger.add_action(AITaskPush(group.id, len(group.tasks)))

                self.m.triggerrules.triggers.append(activation_trigger)
            else:
                group.late_activation = True
                activation_trigger = TriggerOnce(Event.NoEvent, "FlightLateActivationTrigger" + str(group.id))
                activation_trigger.add_condition(TimeAfter(seconds=flight.scheduled_in*60))

                if(flight.from_cp.cptype == ControlPointType.AIRBASE):
                    if flight.from_cp.captured:
                        activation_trigger.add_condition(CoalitionHasAirdrome(self.game.get_player_coalition_id(), flight.from_cp.id))
                    else:
                        activation_trigger.add_condition(CoalitionHasAirdrome(self.game.get_enemy_coalition_id(), flight.from_cp.id))

                if flight.flight_type == FlightType.INTERCEPTION:
                    self.setup_interceptor_triggers(group, flight, activation_trigger)

                activation_trigger.add_action(ActivateGroup(group.id))
                self.m.triggerrules.triggers.append(activation_trigger)

    def setup_interceptor_triggers(self, group, flight, activation_trigger):

        detection_zone = self.m.triggers.add_triggerzone(flight.from_cp.position, radius=25000, hidden=False, name="ITZ")
        if flight.from_cp.captured:
            activation_trigger.add_condition(PartOfCoalitionInZone(self.game.get_enemy_color(), detection_zone.id)) # TODO : support unit type in part of coalition
            activation_trigger.add_action(MessageToAll(String("WARNING : Enemy aircraft have been detected in the vicinity of " + flight.from_cp.name + ". Interceptors are taking off."), 20))
        else:
            activation_trigger.add_condition(PartOfCoalitionInZone(self.game.get_player_color(), detection_zone.id))
            activation_trigger.add_action(MessageToAll(String("WARNING : We have detected that enemy aircraft are scrambling for an interception on " + flight.from_cp.name + " airbase."), 20))

    def generate_planned_flight(self, cp, country, flight:Flight):
        try:
            if flight.client_count == 0 and self.game.settings.perf_ai_parking_start:
                flight.start_type = "Cold"

            if flight.start_type == "In Flight":
                group = self._generate_group(
                    name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                    side=country,
                    unit_type=flight.unit_type,
                    count=flight.count,
                    client_count=0,
                    at=cp.position)
            else:
                st = StartType.Runway
                if flight.start_type == "Cold":
                    st = StartType.Cold
                elif flight.start_type == "Warm":
                    st = StartType.Warm

                if cp.cptype in [ControlPointType.AIRCRAFT_CARRIER_GROUP, ControlPointType.LHA_GROUP]:
                    group_name = cp.get_carrier_group_name()
                    group = self._generate_at_group(
                        name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                        side=country,
                        unit_type=flight.unit_type,
                        count=flight.count,
                        client_count=0,
                        at=self.m.find_group(group_name),
                        start_type=st)
                else:
                    group = self._generate_at_airport(
                        name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                        side=country,
                        unit_type=flight.unit_type,
                        count=flight.count,
                        client_count=0,
                        airport=cp.airport,
                        start_type=st)
        except Exception as e:
            # Generated when there is no place on Runway or on Parking Slots
            logging.error(e)
            logging.warning("No room on runway or parking slots. Starting from the air.")
            flight.start_type = "In Flight"
            group = self._generate_group(
                name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                side=country,
                unit_type=flight.unit_type,
                count=flight.count,
                client_count=0,
                at=cp.position)
            group.points[0].alt = 1500

        flight.group = group
        return group

    @staticmethod
    def configure_behavior(
            group: FlyingGroup,
            react_on_threat: Optional[OptReactOnThreat.Values] = None,
            roe: Optional[OptROE.Values] = None,
            rtb_winchester: Optional[OptRTBOnOutOfAmmo.Values] = None,
            restrict_jettison: Optional[bool] = None) -> None:
        group.points[0].tasks.clear()
        if react_on_threat is not None:
            group.points[0].tasks.append(OptReactOnThreat(react_on_threat))
        if roe is not None:
            group.points[0].tasks.append(OptROE(roe))
        if restrict_jettison is not None:
            group.points[0].tasks.append(OptRestrictJettison(restrict_jettison))
        if rtb_winchester is not None:
            group.points[0].tasks.append(OptRTBOnOutOfAmmo(rtb_winchester))

        group.points[0].tasks.append(OptRTBOnBingoFuel(True))
        group.points[0].tasks.append(OptRestrictAfterburner(True))

    @staticmethod
    def configure_eplrs(group: FlyingGroup, flight: Flight) -> None:
        if hasattr(flight.unit_type, 'eplrs'):
            if flight.unit_type.eplrs:
                group.points[0].tasks.append(EPLRS(group.id))

    def configure_cap(self, group: FlyingGroup, flight: Flight,
                      dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = CAP.name
        self._setup_group(group, CAP, flight, dynamic_runways)

        if flight.unit_type not in GUNFIGHTERS:
            ammo_type = OptRTBOnOutOfAmmo.Values.AAM
        else:
            ammo_type = OptRTBOnOutOfAmmo.Values.Cannon

        self.configure_behavior(group, rtb_winchester=ammo_type)

        group.points[0].tasks.append(EngageTargets(max_distance=nm_to_meter(50),
                                                   targets=[Targets.All.Air]))

    def configure_cas(self, group: FlyingGroup, flight: Flight,
                      dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = CAS.name
        self._setup_group(group, CAS, flight, dynamic_runways)
        self.configure_behavior(
            group,
            react_on_threat=OptReactOnThreat.Values.EvadeFire,
            roe=OptROE.Values.OpenFireWeaponFree,
            rtb_winchester=OptRTBOnOutOfAmmo.Values.Unguided,
            restrict_jettison=True)
        group.points[0].tasks.append(
            EngageTargets(max_distance=nm_to_meter(10),
                          targets=[Targets.All.GroundUnits.GroundVehicles])
        )

    def configure_sead(self, group: FlyingGroup, flight: Flight,
                       dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = SEAD.name
        self._setup_group(group, SEAD, flight, dynamic_runways)
        self.configure_behavior(
            group,
            react_on_threat=OptReactOnThreat.Values.EvadeFire,
            roe=OptROE.Values.OpenFire,
            rtb_winchester=OptRTBOnOutOfAmmo.Values.ASM,
            restrict_jettison=True)

    def configure_strike(self, group: FlyingGroup, flight: Flight,
                         dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = PinpointStrike.name
        self._setup_group(group, GroundAttack, flight, dynamic_runways)
        self.configure_behavior(
            group,
            react_on_threat=OptReactOnThreat.Values.EvadeFire,
            roe=OptROE.Values.OpenFire,
            restrict_jettison=True)

    def configure_anti_ship(self, group: FlyingGroup, flight: Flight,
                            dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = AntishipStrike.name
        self._setup_group(group, AntishipStrike, flight, dynamic_runways)
        self.configure_behavior(
            group,
            react_on_threat=OptReactOnThreat.Values.EvadeFire,
            roe=OptROE.Values.OpenFire,
            restrict_jettison=True)

    def configure_escort(self, group: FlyingGroup, flight: Flight,
                         dynamic_runways: Dict[str, RunwayData]) -> None:
        group.task = Escort.name
        self._setup_group(group, Escort, flight, dynamic_runways)
        self.configure_behavior(group, roe=OptROE.Values.OpenFire,
                                restrict_jettison=True)

    def configure_unknown_task(self, group: FlyingGroup,
                               flight: Flight) -> None:
        logging.error(f"Unhandled flight type: {flight.flight_type.name}")
        self.configure_behavior(group)

    def setup_flight_group(self, group: FlyingGroup, flight: Flight,
                           timing: PackageWaypointTiming,
                           dynamic_runways: Dict[str, RunwayData]) -> None:
        flight_type = flight.flight_type
        if flight_type in [FlightType.CAP, FlightType.BARCAP, FlightType.TARCAP,
                           FlightType.INTERCEPTION]:
            self.configure_cap(group, flight, dynamic_runways)
        elif flight_type in [FlightType.CAS, FlightType.BAI]:
            self.configure_cas(group, flight, dynamic_runways)
        elif flight_type in [FlightType.SEAD, FlightType.DEAD]:
            self.configure_sead(group, flight, dynamic_runways)
        elif flight_type in [FlightType.STRIKE]:
            self.configure_strike(group, flight, dynamic_runways)
        elif flight_type in [FlightType.ANTISHIP]:
            self.configure_anti_ship(group, flight, dynamic_runways)
        elif flight_type == FlightType.ESCORT:
            self.configure_escort(group, flight, dynamic_runways)
        else:
            self.configure_unknown_task(group, flight)

        self.configure_eplrs(group, flight)

        for waypoint in flight.points:
            waypoint.tot = None

        for point in flight.points:
            if point.only_for_player and not flight.client_count:
                continue

            PydcsWaypointBuilder.for_waypoint(
                point, group, flight, timing, self.m
            ).build()

        # Set here rather than when the FlightData is created so they waypoints
        # have their TOTs set.
        self.flights[-1].waypoints = flight.points
        self._setup_custom_payload(flight, group)


class PydcsWaypointBuilder:
    def __init__(self, waypoint: FlightWaypoint, group: FlyingGroup,
                 flight: Flight, timing: PackageWaypointTiming,
                 mission: Mission) -> None:
        self.waypoint = waypoint
        self.group = group
        self.flight = flight
        self.timing = timing
        self.mission = mission

    def build(self) -> MovingPoint:
        waypoint = self.group.add_waypoint(
            Point(self.waypoint.x, self.waypoint.y), self.waypoint.alt)

        waypoint.alt_type = self.waypoint.alt_type
        waypoint.name = String(self.waypoint.name)
        return waypoint

    def set_waypoint_tot(self, waypoint: MovingPoint, tot: int) -> None:
        self.waypoint.tot = tot
        waypoint.ETA = tot
        waypoint.ETA_locked = True
        waypoint.speed_locked = False

    @classmethod
    def for_waypoint(cls, waypoint: FlightWaypoint,
                     group: FlyingGroup,
                     flight: Flight,
                     timing: PackageWaypointTiming,
                     mission: Mission) -> PydcsWaypointBuilder:
        builders = {
            FlightWaypointType.EGRESS: EgressPointBuilder,
            FlightWaypointType.INGRESS_SEAD: SeadIngressBuilder,
            FlightWaypointType.INGRESS_STRIKE: StrikeIngressBuilder,
            FlightWaypointType.JOIN: JoinPointBuilder,
            FlightWaypointType.LANDING_POINT: LandingPointBuilder,
            FlightWaypointType.LOITER: HoldPointBuilder,
            FlightWaypointType.PATROL_TRACK: RaceTrackBuilder,
            FlightWaypointType.SPLIT: SplitPointBuilder,
            FlightWaypointType.TARGET_GROUP_LOC: TargetPointBuilder,
            FlightWaypointType.TARGET_POINT: TargetPointBuilder,
            FlightWaypointType.TARGET_SHIP: TargetPointBuilder,
        }
        builder = builders.get(waypoint.waypoint_type, DefaultWaypointBuilder)
        return builder(waypoint, group, flight, timing, mission)


class DefaultWaypointBuilder(PydcsWaypointBuilder):
    pass


class HoldPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        loiter = ControlledTask(OrbitAction(
            altitude=waypoint.alt,
            pattern=OrbitAction.OrbitPattern.Circle
        ))
        loiter.stop_after_time(
            self.timing.push_time(self.flight, waypoint.position))
        waypoint.add_task(loiter)
        return waypoint


class EgressPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        self.set_waypoint_tot(waypoint, self.timing.egress)
        return waypoint


class IngressBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        self.set_waypoint_tot(waypoint, self.timing.ingress)
        return waypoint


class SeadIngressBuilder(IngressBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()

        target_group = self.waypoint.targetGroup
        if isinstance(target_group, TheaterGroundObject):
            tgroup = self.mission.find_group(target_group.group_identifier)
            if tgroup is not None:
                task = AttackGroup(tgroup.id)
                task.params["expend"] = "All"
                task.params["attackQtyLimit"] = False
                task.params["directionEnabled"] = False
                task.params["altitudeEnabled"] = False
                task.params["weaponType"] = 268402702  # Guided Weapons
                task.params["groupAttack"] = True
                waypoint.tasks.append(task)

        for i, t in enumerate(self.waypoint.targets):
            if self.group.units[0].unit_type == JF_17 and i < 4:
                self.group.add_nav_target_point(t.position, "PP" + str(i + 1))
            if self.group.units[0].unit_type == F_14B and i == 0:
                self.group.add_nav_target_point(t.position, "ST")
            if self.group.units[0].unit_type == AJS37 and i < 9:
                self.group.add_nav_target_point(t.position, "M" + str(i + 1))
        return waypoint


class StrikeIngressBuilder(IngressBuilder):
    def build(self) -> MovingPoint:
        if self.group.units[0].unit_type == B_17G:
            return self.build_bombing()
        else:
            return self.build_strike()

    def build_bombing(self) -> MovingPoint:
        waypoint = super().build()

        targets = self.waypoint.targets
        if not targets:
            return waypoint

        center = Point(0, 0)
        for target in targets:
            center.x += target.position.x
            center.y += target.position.y
        center.x /= len(targets)
        center.y /= len(targets)
        bombing = Bombing(center)
        bombing.params["expend"] = "All"
        bombing.params["attackQtyLimit"] = False
        bombing.params["directionEnabled"] = False
        bombing.params["altitudeEnabled"] = False
        bombing.params["weaponType"] = 2032
        bombing.params["groupAttack"] = True
        waypoint.tasks.append(bombing)
        return waypoint

    def build_strike(self) -> MovingPoint:
        waypoint = super().build()

        for i, t in enumerate(self.waypoint.targets):
            waypoint.tasks.append(Bombing(t.position))
            if self.group.units[0].unit_type == JF_17 and i < 4:
                self.group.add_nav_target_point(t.position, "PP" + str(i + 1))
            if self.group.units[0].unit_type == F_14B and i == 0:
                self.group.add_nav_target_point(t.position, "ST")
            if self.group.units[0].unit_type == AJS37 and i < 9:
                self.group.add_nav_target_point(t.position, "M" + str(i + 1))
        return waypoint


class JoinPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        self.set_waypoint_tot(waypoint, self.timing.join)
        return waypoint


class LandingPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        waypoint.type = "Land"
        waypoint.action = PointAction.Landing
        return waypoint


class RaceTrackBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()

        racetrack = ControlledTask(OrbitAction(
            altitude=waypoint.alt,
            pattern=OrbitAction.OrbitPattern.RaceTrack
        ))

        start = self.timing.race_track_start
        if start is not None:
            self.set_waypoint_tot(waypoint, start)
        racetrack.stop_after_time(self.timing.race_track_end)
        waypoint.add_task(racetrack)
        return waypoint


class SplitPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        self.set_waypoint_tot(waypoint, self.timing.split)
        return waypoint


class TargetPointBuilder(PydcsWaypointBuilder):
    def build(self) -> MovingPoint:
        waypoint = super().build()
        self.set_waypoint_tot(waypoint, self.timing.target)
        return waypoint
