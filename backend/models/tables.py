from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean, Enum
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import TIMESTAMP
import shortuuid
import enum
from backend.database import Base
from backend.models.numeric import Marks

class AssignmentStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    GRADED = "graded"
    LATE = "late"

class EnrollmentStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

class Role(str, enum.Enum):
    STUDENT = "student"
    TA = "ta"
    PROFESSOR = "professor"

class Classroom(Base):
    __tablename__ = "classrooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    subject = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    class_code = Column(String(10), unique=True, index=True, default=lambda: shortuuid.ShortUUID().random(length=6).upper())
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    is_archived = Column(Boolean, default=False)

    # Relationships with passive_deletes for efficient cascade operations
    owner = relationship("User", back_populates="owned_classes")
    enrollments = relationship("Enrollment", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)
    assignments = relationship("Assignment", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)
    announcements = relationship("Announcement", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)
    exams = relationship("Exam", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)
    materials = relationship("Material", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)
    queries = relationship("Query", back_populates="classroom", cascade="all, delete-orphan", passive_deletes=True)

class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    classroom_id = Column(Integer, ForeignKey("classrooms.id", ondelete="CASCADE"))
    status = Column(Enum(EnrollmentStatus), default=EnrollmentStatus.PENDING)
    role = Column(Enum(Role), default=Role.STUDENT)
    joined_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_accessed = Column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    student = relationship("User", back_populates="enrollments")
    classroom = relationship("Classroom", back_populates="enrollments")

class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(TIMESTAMP(timezone=True), nullable=True)
    # Every score in CogniGrade is `Marks` -- NUMERIC(7,2), read back as float.
    # Audit C7: these were Integer, which truncated partial credit on write.
    points_possible = Column(Marks, default=100)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    classroom_id = Column(Integer, ForeignKey("classrooms.id", ondelete="CASCADE"))
    author_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    attachment_path = Column(Text, nullable=True)

    # Relationships
    classroom = relationship("Classroom", back_populates="assignments")
    author = relationship("User")
    submissions = relationship("Submission", back_populates="assignment", cascade="all, delete-orphan", passive_deletes=True)
    materials = relationship("Material", back_populates="assignment", cascade="all, delete-orphan", passive_deletes=True)
    queries = relationship("Query", back_populates="assignment", cascade="all, delete-orphan", passive_deletes=True)
    

class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=True)
    file_path = Column(Text, nullable=True)
    submitted_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    grade = Column(Marks, nullable=True)
    feedback = Column(Text, nullable=True)
    status = Column(Enum(AssignmentStatus), default=AssignmentStatus.PENDING)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"))
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    graded_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    graded_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    assignment = relationship("Assignment", back_populates="submissions")
    student = relationship("User", foreign_keys=[student_id])
    grader = relationship("User", foreign_keys=[graded_by])

class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=lambda: datetime.now(timezone.utc))
    classroom_id = Column(Integer, ForeignKey("classrooms.id", ondelete="CASCADE"))
    author_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    attachment_path = Column(Text, nullable=True)

    classroom = relationship("Classroom", back_populates="announcements")
    author = relationship("User")
    
    materials = relationship("Material", back_populates="announcement", cascade="all, delete-orphan", passive_deletes=True)
    queries = relationship("Query", back_populates="announcement", cascade="all, delete-orphan", passive_deletes=True)

class Exam(Base):
    __tablename__ = "exams"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    exam_date = Column(TIMESTAMP(timezone=True), nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    points_possible = Column(Marks, default=100)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    classroom_id = Column(Integer, ForeignKey("classrooms.id", ondelete="CASCADE"))
    author_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    exam_stage = Column(Integer, default=0)  # e.g., "Question Upload", "Label Extract", "Solution Upload", "Marking Annotate", Answer Script Upload", "Answer Script Annotate", "Grading", "Graded"

    classroom = relationship("Classroom", back_populates="exams")
    author = relationship("User")
    results = relationship("ExamResult", back_populates="exam", cascade="all, delete-orphan", passive_deletes=True)
    materials = relationship("Material", back_populates="exam", cascade="all, delete-orphan", passive_deletes=True)
    queries = relationship("Query", back_populates="exam", cascade="all, delete-orphan", passive_deletes=True)
    answer_scripts = relationship("AnswerScript", back_populates="exam", cascade="all, delete-orphan", passive_deletes=True)
    questions = relationship("Question", back_populates="exam", cascade="all, delete-orphan", passive_deletes=True)

class ExamResult(Base):
    __tablename__ = "exam_results"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"))
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    # NULL still means "grading produced no result" and 0 still means "the
    # student earned nothing" -- Correctness v3 depends on that distinction and
    # widening the type does not touch it.
    marks_obtained = Column(Marks, nullable=True)
    feedback = Column(Text, nullable=True)
    graded_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    graded_at = Column(TIMESTAMP(timezone=True), nullable=True)
    status = Column(Text, nullable=False, default="pending")

    exam = relationship("Exam", back_populates="results")
    student = relationship("User", foreign_keys=[student_id])
    grader = relationship("User", foreign_keys=[graded_by])

class Question(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    question_number = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)        # The question's text or prompt.
    ideal_answer = Column(Text, nullable=True)   # The ideal answer for the question
    ideal_marking_scheme = Column(Text, nullable=True)  # Marking scheme for the ideal answer
    max_marks = Column(Marks, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    part_labels = Column(Text, nullable=True) 

    ms_text_images = Column(Text, nullable=True)
    ms_table_images = Column(Text, nullable=True)
    ms_diagram_images = Column(Text, nullable=True)
    
    exam = relationship("Exam", back_populates="questions")
    responses = relationship("QuestionResponse", back_populates="question", cascade="all, delete-orphan", passive_deletes=True)

class QuestionResponse(Base):
    __tablename__ = "question_responses"
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    answer_text = Column(Text, nullable=True)
    marks_obtained = Column(Marks, nullable=True)
    # Why this response has no mark, when it has none. NULL means "nothing went
    # wrong" -- either the question is graded, or it was never attempted. It is
    # cleared the moment a valid mark is written, so it can never go stale.
    # A provider-neutral code only (see backend/grading/failure.py); the raw
    # model output is logged, never stored, and never shown.
    grading_error_code = Column(Text, nullable=True)
    query = Column(Text, nullable=True)
    reasoning = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    # New columns to store extracted regions JSON data (as a JSON string)
    ans_text_images = Column(Text, nullable=True)
    ans_table_images = Column(Text, nullable=True)
    ans_diagram_images = Column(Text, nullable=True)
    
    question = relationship("Question", back_populates="responses")
    student = relationship("User", back_populates="question_responses")

class DocumentRegion(Base):
    """A structured region of one page of an answer script or marking scheme.

    ADDITIVE. The crop workflow (`QuestionResponse.ans_*_images`,
    `Question.ms_*_images`) is untouched and keeps working; this table is where
    NEW annotations record what those crops could not: which page, where on it,
    what kind of content, whose it is, what order to read it in, and whether a
    human has accepted it. See `backend/regions/schema.py` for the contract.

    Exactly one of `answer_script_id` / `material_id` is set -- the document the
    region lives on. `exam_id` is carried directly so authorization is one join
    rather than three, and so a region can never be orphaned from the exam whose
    policy governs it.
    """

    __tablename__ = "document_regions"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True)
    # The source document. Exactly one is set; enforced in the service layer
    # rather than by a CHECK so the rule lives with the code that explains it.
    answer_script_id = Column(Integer, ForeignKey("answer_scripts.id", ondelete="CASCADE"), nullable=True)
    material_id = Column(Integer, ForeignKey("materials.id", ondelete="CASCADE"), nullable=True)

    #: Page within that document, 0-based. Taken from the REQUEST, never from a
    #: provider's self-report -- see backend/ai/segmentation.py.
    page_index = Column(Integer, nullable=False)

    region_type = Column(Text, nullable=False)
    #: "rect" or "polygon".
    geometry_kind = Column(Text, nullable=False)
    #: JSON, normalised to the page: every coordinate is a float in [0, 1].
    #: Pixel coordinates would not survive a re-render at a different zoom.
    geometry = Column(Text, nullable=False)

    #: Optional semantic assignment. NULL is a legitimate, expected state: a
    #: region whose question is unclear is kept unassigned rather than guessed.
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="SET NULL"), nullable=True)
    question_part = Column(Text, nullable=True)

    #: Explicit ordinal. Never inferred from DOM order or row id.
    reading_order = Column(Integer, nullable=False, default=0)

    #: "proposed" | "accepted" | "modified" | "rejected". A model proposal is
    #: not an annotation until a person says so.
    status = Column(Text, nullable=False, default="proposed")
    #: "model" | "human". Structural only -- never a vendor name.
    source = Column(Text, nullable=False, default="model")

    # Provenance, for later benchmarking. Optional, non-sensitive, and never
    # read by domain logic. No raw provider response, no API key.
    provider = Column(Text, nullable=True)
    model_name = Column(Text, nullable=True)
    prompt_version = Column(Text, nullable=True)
    #: JSON blob of opaque provider metadata (e.g. self-reported confidence,
    #: which is stored but deliberately never acted on).
    provider_metadata = Column(Text, nullable=True)

    #: Optional path to a crop generated from this region. The page plus the
    #: geometry stays authoritative; a crop is a derived artefact that can be
    #: regenerated, which is the inversion this table exists to make possible.
    crop_path = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    exam = relationship("Exam")
    answer_script = relationship("AnswerScript")
    material = relationship("Material")
    question = relationship("Question")


class Query(Base):
    __tablename__ = "queries"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    is_public = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    classroom_id = Column(Integer, ForeignKey("classrooms.id", ondelete="CASCADE"))
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    
    # Multiple foreign keys to support different parent types:
    related_assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=True)
    related_announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=True)
    related_exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=True)
    
    # Self-referential foreign key for responses to another query:
    parent_query_id = Column(Integer, ForeignKey("queries.id", ondelete="CASCADE"), nullable=True)
    
    # Relationships
    classroom = relationship("Classroom", back_populates="queries")
    student = relationship("User")
    assignment = relationship("Assignment", back_populates="queries")
    announcement = relationship("Announcement", back_populates="queries")
    exam = relationship("Exam", back_populates="queries")
    parent_query = relationship("Query", remote_side=[id], back_populates="responses")
    responses = relationship("Query", back_populates="parent_query", cascade="all, delete-orphan", passive_deletes=True)
