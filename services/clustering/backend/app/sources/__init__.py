"""Chain data-source abstraction: the seam between the analysis engine and a
data provider. ``host_ch`` (the host TMS's ingested ClickHouse) is the
implementation today; a node/db-sync adapter drops in behind the same
``ChainSource`` protocol (see docs/online-classification-design.md)."""
