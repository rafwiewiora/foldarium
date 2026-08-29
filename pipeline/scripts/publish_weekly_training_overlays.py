#!/usr/bin/env python3
"""Publish cached exact-scoring training-system overlays to Supabase."""

from foldarium_pipeline.weekly_training_overlays import main

if __name__ == "__main__":
    raise SystemExit(main())
