"""The AI layer: task services, provider-neutral contracts, and one adapter.

    routes / orchestration
            |
    backend.ai.services      task-shaped, provider-neutral
            |
    backend.ai.contracts     the wire between them
            |
    backend.ai.providers     one adapter per provider
            |
    Gemini today, something else tomorrow

Nothing above `providers/` may import a vendor SDK. That single rule is what a
future segmentation/HTR/HMER/grading split depends on.
"""
