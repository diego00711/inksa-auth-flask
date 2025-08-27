#!/bin/bash
# Script to block committing .env files (but allow deletions)

# Check for added or modified .env files (not deletions)
files=$(git diff --cached --name-only --diff-filter=AM | grep -E "(^|/)(\.env(\..*)?$|.*\.env$)" || true)
if [ -n "$files" ]; then
    echo "Error: .env files must not be committed:"
    echo "$files"
    echo "Use .env.example instead and keep .env files local only."
    exit 1
fi