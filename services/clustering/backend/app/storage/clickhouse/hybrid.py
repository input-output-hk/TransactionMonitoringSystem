"""Hybrid repository: host_ch base plus sidecar-local backfilled history.

Selected by ``select_repo_factory`` only when ``HISTORY_SOURCE=blockfrost``.
The history backfill (``service/history.py``) downloads a watched contract's
pre-deployment transactions into the engine's own raw tables — rows the host
never ingested, all strictly below the per-target immutability boundary — and
this repo makes every read span BOTH sources: the windowed hash set, the
engine-shaped transaction columns, and the address co-occurrence pairs each
become a union of a host arm and a local arm.

Invariants the SQL leans on (enforced by the backfill; see service/history.py):

- Disjointness: local rows sit strictly below the host's earliest slot for the
  target, so the two arms never carry the same tx. The local arm still guards
  with ``NOT IN`` the host hashes as insurance, making host precedence explicit
  instead of assumed.
- ``slot`` is the one ordering key present and non-null on both sides (host
  ``transactions.slot`` is Nullable, so the host arm orders by
  ``address_transactions.slot``; the local table stores its own).
- The rolling window (CLUSTERING_WINDOW_TXS) applies over the UNION, newest
  first, so history occupies the TAIL of the window and naturally ages out as
  live traffic accumulates (once the host rows alone fill the window, history
  is no longer read — an accepted property, documented in operations.md).

Writes stay exactly as in ``HostBackedRepo`` (no-ops): the history ingest goes
through a directly-constructed base ``ClickHouseRepo`` instead, so the
request/worker repo can never accidentally download.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from .host_backed import HostBackedRepo


class HybridHistoryRepo(HostBackedRepo):
    """HostBackedRepo whose reads union in the locally-backfilled history."""

    # --- target -> windowed tx_hash subquery (host UNION local) ----------------

    def _hashes_expr(self) -> str:
        """Most recent ``CLUSTERING_WINDOW_TXS`` distinct tx_hashes across the
        host window AND the local history, newest first by slot. The outer
        GROUP BY dedups a hash appearing on both sides (max slot wins).
        ``toString`` on both arms: local ``tx_hash`` is FixedString(64), host is
        String, and a UNION needs one type. Aliases ``s``/``s2`` stay distinct
        from the source column name (ClickHouse 26 alias-shadowing gotcha)."""
        limit = "" if self._window <= 0 else "LIMIT {lim:UInt64}"
        return f"""(
            SELECT tx_hash FROM (
                SELECT tx_hash, max(s) AS s2 FROM (
                    SELECT toString(tx_hash) AS tx_hash, max(slot) AS s
                    FROM {self._host_addr_index()}
                    GROUP BY tx_hash
                    UNION ALL
                    SELECT toString(tx_hash) AS tx_hash, max(slot) AS s
                    FROM {self._db}.transactions FINAL
                    WHERE target = {{tgt:String}}
                    GROUP BY tx_hash
                )
                GROUP BY tx_hash ORDER BY s2 DESC {limit}
            )
        )"""

    # --- engine-shaped transactions (host arm UNION local arm) -----------------

    def _tx_shaped(self, hashes_expr: str) -> str:
        """The engine's transaction column contract for the hashes in
        ``hashes_expr``, from whichever side has them. The local arm projects
        exactly the base repo's shape columns (same names, same net_lovelace
        derivation as ``_SHAPE_FEATURE_SELECT``) so the union unifies
        positionally, and it excludes any hash the host knows — the disjointness
        invariant makes that a no-op, the guard makes host precedence explicit."""
        host_arm = super()._tx_shaped(hashes_expr)
        return f"""(
            SELECT tx_hash, fees, size, input_count, output_count,
                   total_input_lovelace, total_output_lovelace, net_lovelace,
                   distinct_assets, redeemer_count, block_time
            FROM {host_arm}
            UNION ALL
            SELECT
                toString(tx_hash) AS tx_hash,
                fees,
                size,
                input_count,
                output_count,
                total_input_lovelace,
                total_output_lovelace,
                CAST(total_output_lovelace AS Int64)
                    - CAST(total_input_lovelace AS Int64) AS net_lovelace,
                distinct_assets,
                redeemer_count,
                block_time
            FROM {self._db}.transactions FINAL
            WHERE target = {{tgt:String}}
              AND toString(tx_hash) IN {hashes_expr}
              AND toString(tx_hash) NOT IN (
                  SELECT toString(tx_hash) FROM {self._host_addr_index()}
              )
        )"""

    # --- address co-occurrence (host inputs+outputs UNION local tx_utxos) ------

    def _addr_cooccurrence_sql(self, hashes_expr: str, *, order_by: str = "") -> str:
        """DISTINCT (tx_hash, address) over the host inputs+outputs AND the local
        ``tx_utxos`` for the txs in ``hashes_expr``. Each arm stringifies its own
        tx_hash so the union's types line up (see _hashes_expr)."""
        h = self._host_db
        return f"""
            SELECT DISTINCT tx_hash, address FROM (
                SELECT toString(tx_hash) AS tx_hash, address FROM {h}.transaction_outputs
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr} AND address != ''
                UNION DISTINCT
                SELECT toString(tx_hash) AS tx_hash, address FROM {h}.transaction_inputs
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr} AND address != ''
                UNION DISTINCT
                SELECT toString(tx_hash) AS tx_hash, address FROM {self._db}.tx_utxos FINAL
                WHERE target = {{tgt:String}} AND toString(tx_hash) IN {hashes_expr}
                  AND address != ''
            )
            {order_by}
        """

    # --- per-call params --------------------------------------------------------
    # The parent's by-hash reads bind only {net}/{hs}; the hybrid's local arms
    # additionally reference {tgt}, so these overrides exist purely to widen the
    # parameter dicts. fetch_shape_features / fetch_tx_addresses /
    # count_transactions inherit unchanged: they compose the overridden hooks
    # and already bind {tgt} via _scope_params.

    def fetch_shape_features_for(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        if not tx_hashes:
            return pd.DataFrame()
        return self.client.query_df(
            f"SELECT {self._SHAPE_SELECT} FROM {self._tx_shaped('{hs:Array(String)}')} "
            "ORDER BY tx_hash",
            parameters={"net": self._network, "tgt": target, "hs": list(tx_hashes)},
        )

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        if not tx_hashes:
            return pd.DataFrame(columns=["tx_hash", "address"])
        return self.client.query_df(
            self._addr_cooccurrence_sql("{hs:Array(String)}"),
            parameters={"net": self._network, "tgt": target, "hs": list(tx_hashes)},
        )

    # --- history visibility ------------------------------------------------------

    def history_tx_count(self, target: str) -> int:
        # The backfilled subset IS the local table on this repo. (The publish
        # bound, host_known_tx_hashes, is inherited from HostBackedRepo: it
        # queries the HOST index, deliberately ignoring these local rows.)
        return self._local_tx_count(target)
