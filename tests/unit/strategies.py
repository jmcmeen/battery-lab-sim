"""Reusable Hypothesis strategies for property tests.

Composite strategies are exported here so test_ecm_properties /
test_schedule_properties / test_aging_properties can share them.
"""

from __future__ import annotations

from batterylab.chemistry import CHEMISTRIES, ChemistryParams, get_chemistry
from batterylab.schedule import (
    BenchConfig,
    CCStep,
    CVStep,
    CycleConfig,
    EndCondition,
    RestStep,
    Schedule,
    Step,
)
from hypothesis import strategies as st


@st.composite
def chemistry_strategy(draw: st.DrawFn) -> ChemistryParams:
    return get_chemistry(draw(st.sampled_from(sorted(CHEMISTRIES.keys()))))


@st.composite
def end_condition_strategy(draw: st.DrawFn, *, allow_voltage: bool = True) -> EndCondition:
    """At least one stopping rule — never an all-None EndCondition (the
    schedule executor would never advance)."""
    fields: dict[str, float] = {}
    if allow_voltage and draw(st.booleans()):
        fields["voltage_v_above"] = draw(st.floats(min_value=3.6, max_value=4.4))
    if allow_voltage and draw(st.booleans()):
        fields["voltage_v_below"] = draw(st.floats(min_value=2.5, max_value=3.4))
    if draw(st.booleans()):
        fields["current_a_below_c_rate"] = draw(st.floats(min_value=0.01, max_value=0.5))
    if not fields or draw(st.booleans()):
        fields["max_duration_s"] = draw(st.floats(min_value=10.0, max_value=10_000.0))
    return EndCondition(**fields)


@st.composite
def step_strategy(draw: st.DrawFn, *, name: str | None = None) -> Step:
    if name is None:
        name = draw(
            st.text(
                alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
                min_size=1,
                max_size=12,
            )
        )
    kind = draw(st.sampled_from(["rest", "cc", "cv"]))
    if kind == "rest":
        return RestStep(name=name, duration_s=draw(st.floats(min_value=1.0, max_value=3600.0)))
    if kind == "cc":
        rate = draw(
            st.floats(min_value=-3.0, max_value=3.0).filter(lambda r: abs(r) >= 0.05)
        )
        return CCStep(name=name, rate_c=rate, end_when=draw(end_condition_strategy()))
    return CVStep(
        name=name,
        voltage_v=draw(st.floats(min_value=3.0, max_value=4.2)),
        end_when=draw(end_condition_strategy(allow_voltage=False)),
    )


@st.composite
def schedule_strategy(draw: st.DrawFn) -> Schedule:
    chem_name = draw(st.sampled_from(sorted(CHEMISTRIES.keys())))
    n = draw(st.integers(min_value=1, max_value=6))
    # Force unique step names — Schedule rejects duplicates.
    names = [f"step_{i}" for i in range(n)]
    steps = [draw(step_strategy(name=names[i])) for i in range(n)]
    return Schedule(
        schedule_id=draw(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"
                ),
                min_size=1,
                max_size=20,
            )
        ),
        chemistry=chem_name,
        bench=BenchConfig(chassis=[1], channels_per_chassis=1),
        steps=steps,
        cycle=CycleConfig(repeat=draw(st.integers(min_value=1, max_value=10))),
    )
