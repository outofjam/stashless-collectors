#!/usr/bin/env python3
"""Refuses a permission set that is not well-formed XML, grants anything
but reads, or names a projection that exposes secrets or raw message
content. Exit 1 with the reason."""
import sys
import xml.etree.ElementTree as ET

DENIED = {"PsoIntegrationConfiguration", "SMTPEmailServerHandling", "WorkFlowEnvSpecificConfigHandling", "ConnectConfigurationHandling", "ConnectivityInboxMessageHandling", "ConnectivityOutboxMessageHandling"}

path = sys.argv[1]
try:
    root = ET.parse(path).getroot()
except ET.ParseError as error:
    print("permission set: not well-formed XML (%s)" % error)
    sys.exit(1)

text = open(path, encoding="utf-8").read()
projections = [e.text.strip() for e in root.iter("PROJECTION") if e.text]
denied = sorted(set(projections) & DENIED)
if denied:
    print("permission set: grants denied projection(s): %s" % ", ".join(denied))
    sys.exit(1)
if not projections:
    print("permission set: grants no projection at all")
    sys.exit(1)
print("permission set: well-formed, %d projections, none denied" % len(projections))
