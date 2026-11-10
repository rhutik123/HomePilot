import os
from django.core.exceptions import ValidationError

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic bytes (file signatures) to verify actual file content
MAGIC_SIGNATURES = {
    b"%PDF": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"RIFF": "image/webp",  # WebP begins with 'RIFF' followed by 'WEBP'
}


def validate_bill_document(file_obj):
    """
    Validates uploaded bill document against size, extension, and content headers.
    Protects against malicious uploads masquerading as legitimate bills.
    """
    # 1. File size check
    if file_obj.size > MAX_FILE_SIZE_BYTES:
        raise ValidationError(
            f"File size exceeds the 10MB limit. Current size: {file_obj.size / (1024 * 1024):.2f}MB."
        )

    if file_obj.size == 0:
        raise ValidationError("Uploaded file is empty.")

    # 2. File extension check
    _, ext = os.path.splitext(file_obj.name)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed extensions are: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )

    # 3. Magic header inspection (first 16 bytes)
    current_pos = file_obj.tell() if hasattr(file_obj, "tell") else 0
    header = file_obj.read(16)
    if hasattr(file_obj, "seek"):
        file_obj.seek(current_pos)

    is_valid_signature = False
    for sig in MAGIC_SIGNATURES:
        if header.startswith(sig):
            is_valid_signature = True
            break

    if not is_valid_signature:
        raise ValidationError("File content signature does not match a valid PDF or image format.")

    return True
