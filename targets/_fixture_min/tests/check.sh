#!/bin/sh
# Minimal "test runner": writes one JUnit XML and exits on whether the word matches.
set -eu
word="${1:-}"
out="${ORCH_ARTIFACTS_ROOT:-.}/test-results"
mkdir -p "$out"
if [ "$word" = "hello" ]; then
  cat > "$out/hello.xml" <<XML
<testsuite name="hello" tests="1" failures="0" errors="0" skipped="0">
  <testcase name="greets" classname="hello"/>
</testsuite>
XML
  exit 0
fi
cat > "$out/hello.xml" <<XML
<testsuite name="hello" tests="1" failures="1" errors="0" skipped="0">
  <testcase name="greets" classname="hello"><failure message="expected hello"/></testcase>
</testsuite>
XML
exit 1
