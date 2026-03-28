"""SQLAlchemy models for Neural Memory configuration.

Issue #107: Move neural config from env vars to database
"""

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, func

from db.base import Base


class NeuralConfig(Base):
    """Neural Memory configuration stored in database.

    Allows admin-editable configuration for Neural Memory system
    without requiring environment variable changes or restarts.

    Attributes:
        id: Primary key
        key: Configuration parameter name (unique)
        value: Configuration value (stored as string)
        value_type: Type hint for parsing (float, int, bool)
        category: Category for UI grouping
        description: Human-readable description
        min_value: Minimum allowed value (for validation)
        max_value: Maximum allowed value (for validation)
        created_at: Creation timestamp
        updated_at: Last modification timestamp
    """

    __tablename__ = "neural_config"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Configuration key-value
    key = Column(String(64), nullable=False, unique=True, index=True)
    value = Column(String(255), nullable=False)
    value_type = Column(String(16), nullable=False, default="float")
    category = Column(String(32), nullable=False, default="general", index=True)

    # Metadata
    description = Column(Text, nullable=True)  # TEXT to match migration
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<NeuralConfig(key='{self.key}', value='{self.value}')>"

    def get_typed_value(self) -> float | int | bool | str:
        """Get value converted to its proper type."""
        if self.value_type == "float":
            return float(self.value)
        elif self.value_type == "int":
            return int(self.value)
        elif self.value_type == "bool":
            return self.value.lower() in ("true", "1", "yes")
        return self.value

    def validate_value(self, new_value: str) -> tuple[bool, str | None]:
        """Validate a new value against min/max constraints.

        Args:
            new_value: Value to validate (as string)

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            if self.value_type == "float":
                val = float(new_value)
            elif self.value_type == "int":
                val = int(new_value)
            elif self.value_type == "bool":
                # Any string is valid for bool
                return True, None
            else:
                return True, None

            # Check min/max
            if self.min_value is not None and val < self.min_value:
                return False, f"Value must be >= {self.min_value}"
            if self.max_value is not None and val > self.max_value:
                return False, f"Value must be <= {self.max_value}"

            return True, None

        except ValueError as e:
            return False, f"Invalid {self.value_type} value: {e}"
