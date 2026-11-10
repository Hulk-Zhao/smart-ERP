"""枚举 ↔ 代码表：手维护映射必须覆盖全部枚举值。

这些断言的价值不在今天全绿，而在将来：任何人给枚举加了成员却忘了补映射，
差异接口/状态机/失败分类会先在这里变红，而不是等到联调时 500。
"""

from __future__ import annotations

from typing import get_args

from apps.agents.failure import FAILURES
from apps.agents.state_machine import COMPENSATE_FAIL_CODES
from apps.api.service import DIFF_CODE_TO_TYPE, DIFF_TYPE_CODES, FAIL_CODE_OPTIONS
from apps.connectors.errors import (
    _FAIL_CODES,
    _PERMANENT_KINDS,
    _TRANSIENT_KINDS,
    ErrorKind,
)
from apps.platform.contracts.errors import FailCode
from apps.platform.contracts.ticket import TICKET_STATES, TRANSITIONS, TicketState
from apps.recon.models import DiffType

PLATFORM_FAIL_CODES = frozenset(get_args(FailCode))


def test_diff_type_codes_cover_every_enum_member():
    assert set(DiffType) <= set(DIFF_TYPE_CODES)
    assert set(DIFF_TYPE_CODES) == set(DiffType)
    assert DIFF_TYPE_CODES[DiffType.ROUNDING_TOLERANCE] == "rounding_tolerance"

    codes = list(DIFF_TYPE_CODES.values())
    assert len(codes) == len(set(codes))
    assert all(code.isascii() and code == code.lower() for code in codes)
    assert DIFF_CODE_TO_TYPE == {
        code: diff_type for diff_type, code in DIFF_TYPE_CODES.items()
    }


def test_ticket_state_tables_cover_all_twelve_states():
    assert set(TRANSITIONS) == set(TicketState)
    assert set(TICKET_STATES) == {state.value for state in TicketState}
    assert len(TicketState) == 12
    for targets in TRANSITIONS.values():
        assert targets <= set(TicketState)


def test_fail_code_tables_cover_all_f_codes():
    assert set(FAIL_CODE_OPTIONS) <= PLATFORM_FAIL_CODES
    assert set(FAIL_CODE_OPTIONS) == set(COMPENSATE_FAIL_CODES)
    assert set(FAILURES) == PLATFORM_FAIL_CODES


def test_connector_error_kind_tables_cover_all_kinds():
    assert set(_FAIL_CODES) == set(ErrorKind)
    assert {code for code in _FAIL_CODES.values() if code is not None} <= PLATFORM_FAIL_CODES
    assert _TRANSIENT_KINDS | _PERMANENT_KINDS <= set(ErrorKind)
