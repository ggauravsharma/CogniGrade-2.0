"""Authorization layer for CogniGrade.

`policies`  reusable FastAPI dependencies and assertions for role, ownership
            and enrolment checks against exams, questions and responses.
`files`     authorized serving of uploaded files, replacing the public
            StaticFiles mount.
"""
