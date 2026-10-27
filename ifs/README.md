# The IFS Cloud permission set

`CMDB_READ-permission-set.xml` is the permission set every collector's IAM client needs, the same one a Stashless-connected non-production environment uses. It grants read access to the projections the collector reads and nothing else: no actions, no writes, and none of the projections that expose passwords, tokens or raw message content.

Import it in IFS Cloud under Users and Permissions, Permission Sets, Import, then grant `CMDB_READ` to the IAM client's service user. The set is called `CMDB_READ` inside IFS (the product's earlier name, kept so an environment imported before the rename still matches) while the Stashless guides and the collector call the IAM client `STASHLESS_READ`; they are the same grant. The Stashless setup guide, "Connect IFS Cloud", walks through the IAM client itself.

Stashless serves the same file at `https://stashless.outofjam.com/ifs/CMDB_READ-permission-set.xml`; the copy here is published from it so the two never differ, and the conformance check refuses a copy that grants a denied projection.
