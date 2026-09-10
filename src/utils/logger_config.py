"""
Enterprise-grade logging configuration
Provides structured logging with rotation, multiple handlers, and contextual information
"""
import logging
import logging.handlers
import json
import re
import shutil
import sys
import os
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path


# Timestamp of the moment this process started, used to give every run its own
# log directory instead of appending to files shared with previous runs.
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Matches a run directory name (e.g. 20260910_143005)
_RUN_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def run_log_dir(log_dir: str, run_timestamp: Optional[str] = None) -> Path:
    """
    Get (and create) the directory holding this run's log files

    Args:
        log_dir: Root directory for log files
        run_timestamp: Timestamp naming the directory; defaults to this run's

    Returns:
        Path of the run directory (e.g. logs/20260910_143005)
    """
    run_path = Path(log_dir) / (run_timestamp or RUN_TIMESTAMP)
    run_path.mkdir(parents=True, exist_ok=True)
    return run_path


def prune_old_run_logs(log_dir: str, keep_runs: int) -> None:
    """
    Delete the log directories of older runs, keeping the newest ones

    Args:
        log_dir: Root directory for log files
        keep_runs: Number of most recent runs to keep (0 or less disables pruning)
    """
    if keep_runs <= 0:
        return

    log_path = Path(log_dir)
    if not log_path.is_dir():
        return

    run_dirs = sorted(
        entry for entry in log_path.iterdir()
        if entry.is_dir() and _RUN_DIR_RE.match(entry.name)
    )

    for run_dir in run_dirs[:-keep_runs]:
        try:
            shutil.rmtree(run_dir)
        except OSError:
            # A log directory we cannot delete is not worth failing startup over
            pass


class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging"""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
            "process_id": record.process,
            "thread_id": record.thread,
        }
        
        # Add exception information if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields if present
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        # Add correlation_id if present
        if hasattr(record, "correlation_id"):
            log_data["correlation_id"] = record.correlation_id
        
        # Add alert_id if present
        if hasattr(record, "alert_id"):
            log_data["alert_id"] = record.alert_id
        
        # Add task_id if present
        if hasattr(record, "task_id"):
            log_data["task_id"] = record.task_id
        
        # Add performance metrics if present
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        
        return json.dumps(log_data, ensure_ascii=False)


class ContextualFormatter(logging.Formatter):
    """Enhanced formatter with contextual information"""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record with context"""
        # Base format
        base_format = (
            "%(asctime)s | %(levelname)-8s | %(name)s | "
            "%(module)s.%(funcName)s:%(lineno)d | %(message)s"
        )
        
        # Add context if available
        context_parts = []
        if hasattr(record, "correlation_id"):
            context_parts.append(f"corr_id={record.correlation_id}")
        if hasattr(record, "alert_id"):
            context_parts.append(f"alert_id={record.alert_id}")
        if hasattr(record, "task_id"):
            context_parts.append(f"task_id={record.task_id}")
        if hasattr(record, "duration_ms"):
            context_parts.append(f"duration={record.duration_ms}ms")
        
        if context_parts:
            context_str = " | " + " | ".join(context_parts)
        else:
            context_str = ""
        
        # Format message
        msg = super().format(record)
        if context_str:
            msg = msg + context_str
        
        # Add exception if present
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        
        return msg


class ContextualLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that adds contextual information"""
    
    def __init__(self, logger: logging.Logger, context: Optional[Dict[str, Any]] = None):
        super().__init__(logger, context or {})
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        """Add context to log records"""
        extra = kwargs.get("extra", {})
        
        # Merge context into extra
        if self.extra:
            extra.update(self.extra)
        
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(
    log_level: str = "INFO",
    log_dir: str = "logs",
    log_file: str = "alert_processor.log",
    json_logging: bool = False,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 10,
    verbose: bool = False,
    run_timestamp: Optional[str] = None,
    keep_runs: int = 30
) -> logging.Logger:
    """
    Setup enterprise-grade logging configuration
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_dir: Root directory for log files; each run gets a timestamped subdirectory
        log_file: Name of the log file
        json_logging: Enable JSON structured logging
        max_bytes: Maximum size of log file before rotation
        backup_count: Number of backup log files to keep
        verbose: Enable verbose (DEBUG) logging
        run_timestamp: Timestamp naming this run's log directory; defaults to this run's
        keep_runs: Number of past runs whose logs are kept (0 or less keeps all)
        
    Returns:
        Configured logger instance
    """
    # Determine log level
    if verbose:
        level = logging.DEBUG
    else:
        level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Give this run its own directory under the log root
    log_path = run_log_dir(log_dir, run_timestamp)
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    # In verbose mode, show everything (DEBUG and above); otherwise only show errors
    if verbose:
        console_handler.setLevel(level)
    else:
        console_handler.setLevel(logging.ERROR)
    
    if json_logging:
        console_formatter = JSONFormatter()
    else:
        console_formatter = ContextualFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
    
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # File handler with rotation
    log_file_path = log_path / log_file
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    file_handler.setLevel(level)
    
    if json_logging:
        file_formatter = JSONFormatter()
    else:
        file_formatter = ContextualFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
    
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    
    # Error file handler (separate file for errors)
    error_log_file = log_path / f"error_{log_file}"
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(file_formatter)
    root_logger.addHandler(error_handler)
    
    # Drop the logs of the oldest runs, this one included in the count
    prune_old_run_logs(log_dir, keep_runs)
    
    # Suppress noisy third-party loggers
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    
    logger = logging.getLogger(__name__)
    logger.info(
        f"Logging initialized: level={logging.getLevelName(level)}, "
        f"log_dir={log_path}, log_file={log_file}, json_logging={json_logging}"
    )
    
    return logger


def get_logger(name: str, context: Optional[Dict[str, Any]] = None) -> ContextualLoggerAdapter:
    """
    Get a logger with optional context
    
    Args:
        name: Logger name (typically __name__)
        context: Optional context dictionary (correlation_id, alert_id, etc.)
        
    Returns:
        ContextualLoggerAdapter instance
    """
    logger = logging.getLogger(name)
    return ContextualLoggerAdapter(logger, context)


class PerformanceLogger:
    """Context manager for performance logging"""
    
    def __init__(self, logger: logging.Logger, operation: str, **context):
        self.logger = logger
        self.operation = operation
        self.context = context
        self.start_time = None
    
    def __enter__(self):
        self.start_time = datetime.utcnow()
        self.logger.debug(f"Starting {self.operation}", extra=self.context)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration = (datetime.utcnow() - self.start_time).total_seconds() * 1000
            duration_ms = int(duration)
            
            context = self.context.copy()
            context["duration_ms"] = duration_ms
            
            if exc_type is None:
                self.logger.info(
                    f"Completed {self.operation}",
                    extra=context
                )
            else:
                self.logger.error(
                    f"Failed {self.operation}",
                    extra=context,
                    exc_info=(exc_type, exc_val, exc_tb)
                )

