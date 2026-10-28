"""Exit codes of the commands, and the epilog that lists them."""

EXIT_OK = 0
EXIT_ERROR = 1  # GitHub, Git, or registry failure
EXIT_USAGE = 2  # argparse
EXIT_REFUSED = 4  # invalid task, or the Issue prohibits execution
EXIT_PLANNING_FAILED = 7  # planner failed, or returned an unusable plan
EXIT_TASK_FAILED = 8  # a check caught a problem; nothing was committed
EXIT_TASK_NEEDS_REVIEW = 9  # the task could not be judged; a person must look
EXIT_INTERRUPTED = 130  # Ctrl+C, as a shell reports it

EPILOG = """\
exit codes:
  0 success   1 error   2 usage
  4 task invalid or execution prohibited
  7 AI planning failed or returned an invalid plan
  8 task failed verification   9 task needs review
"""
