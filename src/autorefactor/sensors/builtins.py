from autorefactor.sensors.cluster import ClusterSensor
from autorefactor.sensors.complexity import ComplexitySensor
from autorefactor.sensors.contract import ContractSensor
from autorefactor.sensors.coverage import CoverageSensor
from autorefactor.sensors.fingerprint import FingerprintCloneSensor
from autorefactor.sensors.placement import PlacementSensor
from autorefactor.sensors.similarity import ClassSimilaritySensor, MethodSimilaritySensor
from autorefactor.sensors.slop import SlopSensor
from autorefactor.sensors.static_adapters import (
    EslintSensor,
    JavaStaticSensor,
    RuffSensor,
    SemgrepSensor,
)
from autorefactor.sensors.structure import StructureSensor
from autorefactor.sensors.supabase import SupabaseSensor
from autorefactor.sensors.test_quality import TestQualitySensor
from autorefactor.sensors.wiring import WiringSensor


def builtin_sensors():
    return [
        MethodSimilaritySensor(),
        ClassSimilaritySensor(),
        FingerprintCloneSensor(),
        SlopSensor(),
        PlacementSensor(),
        StructureSensor(),
        ComplexitySensor(),
        CoverageSensor(),
        TestQualitySensor(),
        ContractSensor(),
        ClusterSensor(),
        WiringSensor(),
        SupabaseSensor(),
        SemgrepSensor(),
        RuffSensor(),
        EslintSensor(),
        JavaStaticSensor(),
    ]


# Entry-point factories
def sim_method():
    return MethodSimilaritySensor()


def sim_class():
    return ClassSimilaritySensor()


def sim_fingerprint():
    return FingerprintCloneSensor()


def slop():
    return SlopSensor()


def placement():
    return PlacementSensor()


def structure():
    return StructureSensor()


def complexity():
    return ComplexitySensor()


def coverage():
    return CoverageSensor()


def test_quality():
    return TestQualitySensor()


def contract():
    return ContractSensor()


def cluster():
    return ClusterSensor()


def wiring():
    return WiringSensor()


def supabase():
    return SupabaseSensor()


def static_semgrep():
    return SemgrepSensor()


def static_ruff():
    return RuffSensor()


def static_eslint():
    return EslintSensor()


def static_java():
    return JavaStaticSensor()
