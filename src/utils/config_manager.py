"""
Configuration management for loading and parsing config files
"""
import configparser
import os
from typing import Dict, Any, Optional
from src.utils.logger_config import get_logger


def load_config(config_file="config/config.conf") -> configparser.ConfigParser:
    """Load configuration from config file"""
    config = configparser.ConfigParser()
    config.read(config_file)
    return config


def parse_config(config: configparser.ConfigParser, api_client) -> Dict[str, Any]:
    """
    Parse configuration and return structured dictionary
    
    Args:
        config: ConfigParser object with config file contents
        api_client: APIClient instance for fetching global settings
        
    Returns:
        Dictionary with parsed configuration values
        
    Raises:
        ValueError: If required configuration is missing
    """
    logger = get_logger(__name__)
    parsed = {}
    
    # Fetch global settings from API
    global_settings = api_client.get_global_settings()
    
    try:
        # AWS Configuration - prefer API settings, fallback to environment variable, then config
        if global_settings and "AWS" in global_settings:
            aws_settings = global_settings["AWS"]
            parsed["aws_region"] = aws_settings.get("default-region", "").strip()
            # Set AWS credentials from API response
            if aws_settings.get("access-key"):
                os.environ["AWS_ACCESS_KEY_ID"] = aws_settings["access-key"]
            if aws_settings.get("secret-key"):
                os.environ["AWS_SECRET_ACCESS_KEY"] = aws_settings["secret-key"]
            if parsed["aws_region"]:
                os.environ["AWS_DEFAULT_REGION"] = parsed["aws_region"]
            logger.info("AWS credentials loaded from global settings API")
        
        # Fallback to environment variable if not from API
        if not parsed.get("aws_region"):
            aws_region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
            if not aws_region:
                raise ValueError("AWS region not found! Please set AWS_DEFAULT_REGION or AWS_REGION")
            parsed["aws_region"] = aws_region.strip()
        
        parsed["s3_bucket"] = config.get("AWS", "S3_BUCKET").strip()
        parsed["s3_upload_prefix_template"] = config.get("AWS", "S3_UPLOAD_PREFIX", fallback="alerts/").strip()
        
        # Clip Configuration - must come from global settings API
        if not global_settings or "ALERT_PROCESSOR" not in global_settings:
            raise ValueError("ALERT_PROCESSOR settings not found in global settings API response")
        alert_processor_settings = global_settings["ALERT_PROCESSOR"]
        parsed["before_seconds"] = int(alert_processor_settings["before-seconds"])
        parsed["after_seconds"] = int(alert_processor_settings["after-seconds"])
        logger.info("Alert processor settings loaded from global settings API")
        output_dir = config.get("CLIP", "OUTPUT_DIR").strip()
        if not output_dir:
            raise ValueError("OUTPUT_DIR is empty in config.conf! Please set OUTPUT_DIR to a valid directory path")
        parsed["output_dir"] = os.path.normpath(
            os.path.expanduser(os.path.expandvars(output_dir))
        )
        parsed["chunk_duration_seconds"] = int(config.get("CLIP", "CHUNK_DURATION_SECONDS", fallback="300").strip())
        chunk_filename_pattern = config.get("CLIP", "CHUNK_FILENAME_PATTERN", fallback=None)
        parsed["chunk_filename_pattern"] = chunk_filename_pattern.strip() if chunk_filename_pattern else None
        
        local_source_dir = config.get("CLIP", "LOCAL_SOURCE_DIR", fallback=None)
        if not local_source_dir:
            raise ValueError("LOCAL_SOURCE_DIR not found in config.conf! Please add LOCAL_SOURCE_DIR to the [CLIP] section")
        local_source_dir = local_source_dir.strip()
        if not local_source_dir:
            raise ValueError("LOCAL_SOURCE_DIR is empty in config.conf! Please set LOCAL_SOURCE_DIR to a valid directory path")
        parsed["local_source_dir"] = os.path.normpath(
            os.path.expanduser(os.path.expandvars(local_source_dir))
        )
        
        # Processing Configuration
        parsed["max_retries"] = int(config.get("PROCESSING", "MAX_RETRIES", fallback="3").strip())
        parsed["retry_delay_seconds"] = int(config.get("PROCESSING", "RETRY_DELAY_SECONDS", fallback="2").strip())
        
        # API Configuration
        api_base_url = config.get("API", "BASE_URL", fallback=None)
        if not api_base_url:
            raise ValueError("BASE_URL not found in config.conf! Please add BASE_URL to the [API] section")
        parsed["api_base_url"] = api_base_url.strip()
        parsed["alerts_endpoint"] = config.get("API", "ALERTS_ENDPOINT").strip()
        parsed["secondary_video_endpoint"] = config.get("API", "SECONDARY_VIDEO_ENDPOINT").strip()
        
        # Email Configuration - prefer API settings, fallback to config file
        if global_settings and "MAIL" in global_settings:
            mail_settings = global_settings["MAIL"]
            parsed["email_enabled"] = True
            parsed["from_email"] = mail_settings.get("username", "").strip()
            recipients_str = mail_settings.get("receipients", "").strip()
            parsed["to_emails"] = [email.strip() for email in recipients_str.split(',') if email.strip()]
            parsed["use_tls"] = True  # Default to True
            # Set SMTP settings in environment for EmailSender
            if mail_settings.get("server"):
                os.environ["SMTP_SERVER"] = mail_settings["server"]
            if mail_settings.get("port"):
                os.environ["SMTP_PORT"] = str(mail_settings["port"])
            if mail_settings.get("username"):
                os.environ["SMTP_USERNAME"] = mail_settings["username"]
            if mail_settings.get("password"):
                os.environ["SMTP_PASSWORD"] = mail_settings["password"]
            logger.info("Email settings loaded from global settings API")
        else:
            # Fallback to config file
            parsed["email_enabled"] = config.getboolean("EMAIL", "ENABLED", fallback=False)
            if parsed["email_enabled"]:
                parsed["from_email"] = config.get("EMAIL", "FROM_EMAIL").strip()
                to_emails_str = config.get("EMAIL", "TO_EMAILS").strip()
                parsed["to_emails"] = [email.strip() for email in to_emails_str.split(',')]
                parsed["use_tls"] = config.getboolean("EMAIL", "USE_TLS", fallback=True)
        
        # MQTT Broker Configuration - prefer API settings, fallback to environment variables
        if global_settings and "BROKER" in global_settings:
            broker_settings = global_settings["BROKER"]
            # Set MQTT settings in environment for status_manager
            if broker_settings.get("host"):
                os.environ["MQTT_HOST"] = broker_settings["host"].strip()
            if broker_settings.get("port"):
                os.environ["MQTT_PORT"] = str(broker_settings["port"]).strip()
            if broker_settings.get("username"):
                os.environ["MQTT_USER"] = broker_settings["username"].strip()
            if broker_settings.get("password"):
                os.environ["MQTT_PASS"] = broker_settings["password"].strip()
            logger.info("MQTT broker settings loaded from global settings API")
        
        logger.info("Configuration parsed successfully", extra={
            "s3_bucket": parsed["s3_bucket"],
            "aws_region": parsed["aws_region"],
            "email_enabled": parsed["email_enabled"]
        })
        
        return parsed
        
    except Exception as e:
        logger.error(f"Failed to parse configuration: {e}", exc_info=True)
        raise

