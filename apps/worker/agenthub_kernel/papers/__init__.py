"""P6 paper research pipeline (``docs/p6-paper-pipeline.md``).

The pipeline is a stage plan (``agenthub.stage.PLAN_PAPER``) driven by the P4.6
:class:`~agenthub_kernel.autopilot.Autopilot`: model steps are stage calls
through the usual consume gate; tool steps are checked against the task's
resource rules (``agenthub.papers.ResourceRules``) right before they run.
"""
