# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and GitHub Copilot CLI when working in this repository.

`crs/`, `fed/`, `local-laws/` and `scotus/` are independent Python pipelines; `README.md` links their sources, datasets and commands.

Read `<folder>/CLAUDE.md` before working in that folder. Work in one folder must not change another.

Workflows live only in the root `.github/workflows/`, prefixed by folder. Renaming a workflow file breaks its datasets' Trusted Publisher registrations, so never rename one without re-registering it on Hugging Face; see `README.md` under Publishing.

Never run `run` or `squash` without `--local` unless the owner asks.

The repository is public.
