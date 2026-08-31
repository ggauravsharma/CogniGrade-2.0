from fastapi import Form, APIRouter, HTTPException, Depends, UploadFile, File, Body, status
from backend.database import get_db
from backend.models.users import User, UserSettings
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession     # ASYNC
from sqlalchemy.future import select
import logging

from backend.utils.security import get_current_user_required, get_password_hash, verify_password
from typing import Optional
import os
from io import BytesIO
from PIL import Image

router = APIRouter(tags=["profile-settings"])
logger = logging.getLogger(__name__)
PROFILE_PICTURE_DIR = "./profile_pictures"

# Ensure the profile pictures directory exists
os.makedirs(PROFILE_PICTURE_DIR, exist_ok=True)

@router.get("/get-info")
async def get_info(current_user: User = Depends(get_current_user_required)):
    return {
        "user": {
            "full_name": current_user.full_name,
            "is_professor": current_user.is_professor,
            "email": current_user.email,
            "bio": current_user.bio if current_user.bio else "",
            "profile_picture": current_user.profile_picture  # Return the file path if the picture exists
        }
    }

@router.post("/update-profile")
async def update_profile(
    full_name: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    bio: Optional[str] = Form(None),
    profile_picture: UploadFile = File(None),
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Updates the user's profile information and profile picture."""
    
    # Check if a profile picture is uploaded
    if profile_picture:
        file_location = f"./profile_pictures/{current_user.id}.jpg"
        try:
            # Process image to maintain quality
            img_contents = await profile_picture.read()
            img = Image.open(BytesIO(img_contents))
            
            # Convert to RGB if in RGBA mode
            if img.mode == 'RGBA':
                img = img.convert('RGB')
                
            # Save with high quality
            img.save(file_location, "JPEG", quality=95)

            print(f"Profile picture saved at {file_location}")
            current_user.profile_picture = file_location
            await db.commit()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Error processing profile image: {str(e)}"
            )

    # Update the user's profile in the database
    if full_name:
        current_user.full_name = full_name
    if email:
        current_user.email = email
    if bio:
        current_user.bio = bio
    await db.commit()
    
    return {"message": "Profile updated successfully"}

@router.post("/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Changes the user's password."""
    
    # Verify current password
    if not verify_password(current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )
    
    # Verify new password matches confirmation
    if new_password != confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password and confirmation do not match"
        )
    
    # Validate password strength (can add more rules as needed)
    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long"
        )
    
    # Update password
    current_user.hashed_password = get_password_hash(new_password)
    await db.commit()
    
    return {"message": "Password changed successfully"}

@router.get("/notification-settings")
async def get_notification_settings(
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Gets the user's notification settings."""
    
    # Get or create user settings
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalars().first()
    
    if not user_settings:
        user_settings = UserSettings(
            user_id=current_user.id,
            email_notifications=True,
            display_theme="light",
            language_preference="en"
        )
        db.add(user_settings)
        await db.commit()
        await db.refresh(user_settings)
    
    return {
        "email_notifications": user_settings.email_notifications,
        "display_theme": user_settings.display_theme,
        "language_preference": user_settings.language_preference
    }

@router.post("/notification-settings")
async def update_notification_settings(
    email_notifications: bool = Form(...),
    display_theme: str = Form(...),
    language_preference: str = Form(...),
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Updates the user's notification settings."""
    
    # Get or create user settings
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalars().first()
    
    if not user_settings:
        user_settings = UserSettings(
            user_id=current_user.id,
            email_notifications=email_notifications,
            display_theme=display_theme,
            language_preference=language_preference
        )
        db.add(user_settings)
    else:
        user_settings.email_notifications = email_notifications
        user_settings.display_theme = display_theme
        user_settings.language_preference = language_preference
    
    await db.commit()
    
    return {"message": "Notification settings updated successfully"}

@router.get("/privacy-settings")
async def get_privacy_settings(
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Gets the user's privacy settings."""
    
    # For now, return basic privacy settings
    # This could be extended with a dedicated PrivacySettings model in the future
    return {
        "profile_visibility": "public",  # Example field
        "activity_visibility": "followers"  # Example field
    }

@router.post("/privacy-settings")
async def update_privacy_settings(
    profile_visibility: str = Form(...),
    activity_visibility: str = Form(...),
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Updates the user's privacy settings."""
    
    # Placeholder for future privacy settings implementation
    # This could save to a dedicated PrivacySettings model
    
    return {"message": "Privacy settings updated successfully"}

@router.post("/delete-account")
async def delete_account(
    password: str = Form(...),
    current_user: User = Depends(get_current_user_required),
    db: AsyncSession = Depends(get_db)
):
    """Permanently deletes the user's account.

    WHAT WAS WRONG
    --------------
    The previous implementation walked `current_user.answer_scripts`,
    `.question_responses`, `.enrollments` and `.received_notifications` -- four
    LAZY relationships -- inside async context, so the route raised before it
    deleted anything. Behind that sat a second defect: six `db.delete(...)`
    calls and a `db.rollback()` with no `await`. `AsyncSession.delete` is a
    coroutine; unawaited it builds an object and discards it. Repairing only
    the lazy loads would therefore have produced the worse failure -- a 200
    response for an account that was never deleted.

    WHY A CORE DELETE
    -----------------
    `await db.delete(user)` would still be wrong here. Most of `User`'s
    relationships lack `passive_deletes=True`, so the ORM would load every
    child at flush time to null out its foreign key -- the same lazy IO in the
    same async context, just moved. A Core `DELETE` statement bypasses ORM
    relationship processing entirely: one statement, no loading, no surprises.

    WHO DELETES THE REST
    --------------------
    The database. Every foreign key pointing at `users.id` is declared
    `ON DELETE CASCADE` (17 of them across 12 tables), so the schema already
    states the policy and duplicating it in Python would mean maintaining a
    second, silently divergent copy. See `backend/database.py` for why SQLite
    now enforces those cascades too.

    Note the blast radius that policy implies: `classrooms.owner_id` cascades,
    so deleting a professor's account destroys their classrooms and everything
    inside them. That is the existing schema's rule, not a choice made here.
    """

    # Verify password for security
    if not verify_password(password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password is incorrect"
        )

    # Read what is needed BEFORE the row goes away. `id` is an int from the
    # database, so the filename it builds cannot escape the pictures directory.
    user_id = current_user.id
    picture_path = os.path.join(PROFILE_PICTURE_DIR, f"{user_id}.jpg")

    try:
        # Detach the ORM instance first: nothing must try to flush or cascade
        # it while the Core statement does the work.
        db.expunge(current_user)
    except Exception:
        pass

    try:
        await db.execute(sa_delete(User).where(User.id == user_id))
        await db.commit()
    except Exception:
        await db.rollback()
        # The detail is for the logs. Echoing str(e) to the client leaked
        # driver messages, which can carry connection details.
        logger.error("account deletion failed for user_id=%s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting account"
        )

    # Only once the deletion is durable. Doing this first, as the old code did,
    # destroyed the picture even when the account survived. Best effort: a file
    # that cannot be removed must not turn a completed deletion into an error.
    try:
        if os.path.exists(picture_path):
            os.remove(picture_path)
    except OSError:
        logger.warning("could not remove profile picture for deleted user_id=%s", user_id)

    return {"message": "Account deleted successfully"}