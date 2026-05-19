#!/usr/bin/env python3
"""asf-follow — thin wrapper that delegates to audio_score_follower.main."""

from audio_score_follower.main import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
