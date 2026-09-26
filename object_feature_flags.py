"""The two gates every optional pass reads: an operator opt-in in the
environment, and a brownout kill switch in the ``feature_flags`` collection.

Extracted, not invented. The kill switch below existed as six word-for-word
copies (object_rollups, object_notify, object_email, object_connectors,
object_materialize, and app-timers' timer_actions), each docstring saying it
"mirrors" another one exactly. Doctrine #4 says to extract a primitive after
the pattern repeats; six is well past that, and mail sync was about to make
it seven. The five root modules keep their own public names
(``email_pass_enabled`` and friends) because callers and tests import those;
only the body moved here. app-timers' copy lives in a package object and is
left for that package's next version, since changing an installed object is
a package upgrade rather than a refactor.

The two gates answer different questions and must not be merged:

- ``env_opt_in`` is **adoption**: OFF unless the operator wrote the variable
  into the environment file. A capability that talks to the outside world
  (an SMTP relay, an IMAP server, a WebDAV mount) is something an operator
  chooses, never something a fresh box does on its own.
- ``kill_switch_on`` is **brownout**: ON unless a ``feature_flags`` row says
  off. It exists so an operator can stop a misbehaving pass from the admin
  UI without a redeploy. A missing row, a missing or unreadable collection
  and a blank value all read as "on"; only an explicit off/false/0/no turns
  it off -- a kill switch that failed closed on a missing table would turn
  every fresh box's passes off for a reason nobody chose.

``refusal`` combines them into the one sentence a caller reports, naming
exactly what to set (AGENTS.md: a capability that is absent refuses, and
the refusal names what to configure).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import object_collections
import object_records

FEATURE_FLAGS_COLLECTION = "feature_flags"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
OFF_VALUES = frozenset({"off", "false", "0", "no"})


def env_opt_in(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True only when the environment variable says 1/true/yes/on."""
    source = os.environ if env is None else env
    return str(source.get(name, "") or "").strip().lower() in TRUE_VALUES


def kill_switch_on(flag: str, *, base_dir: Path | str | Any) -> bool:
    """True unless a ``feature_flags`` row named ``flag`` says off/false/0/no.

    The first row carrying the flag decides, as every copy this replaced
    did.
    """
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return True
    for row in rows:
        if row.get("flag") == flag:
            value = (row.get("value") or "").strip().lower()
            return True if not value else value not in OFF_VALUES
    return True


def refusal(
    *,
    env_name: str | None = None,
    flag: str | None = None,
    base_dir: Path | str | Any,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """None when the feature may run; otherwise the sentence saying why not,
    naming the variable or the flag row that would change the answer."""
    if env_name and not env_opt_in(env_name, env):
        return f"off: set {env_name}=true in the server's environment to turn it on"
    if flag and not kill_switch_on(flag, base_dir=base_dir):
        return (f"off: the feature_flags row {flag!r} is set to off "
                f"(an operator's kill switch); remove it or set it to on")
    return None
