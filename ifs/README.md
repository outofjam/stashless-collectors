# IFS Cloud permission set

`STASHLESS_READ-permission-set.xml` is the permission set every collector's IAM client needs, the same one a Stashless-connected non-production environment uses. It grants read access to the projections the collector reads and nothing else: no actions, no writes, and none of the projections that expose passwords, tokens or raw message content.

Download it (the **Download raw file** button at the top right of the file view, or **Raw** and save the page as `STASHLESS_READ-permission-set.xml`), then import it in IFS Cloud under **Users and Permissions** › **Permission Sets** › **Import**, and grant `STASHLESS_READ` to the IAM client's service user. The IAM client is called `STASHLESS_READ` too, on purpose. The Stashless guide "Get started", step 4, walks through the IAM client itself.

The set was called `CMDB_READ` before late October 2026. If you imported it under that name, importing this file creates `STASHLESS_READ` beside it: grant `STASHLESS_READ` to the service user and delete `CMDB_READ`, or keep the old set and nothing breaks.

Stashless serves the same file at `https://stashless.outofjam.com/ifs/STASHLESS_READ-permission-set.xml`; the copy here is published from it so the two never differ, and the conformance check refuses a copy that grants a denied projection.
