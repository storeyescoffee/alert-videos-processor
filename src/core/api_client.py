"""
API Client for interacting with the alerts API
Handles fetching alerts and updating alert secondary video URLs
"""
import os
import requests
import logging
from typing import List, Dict, Optional
from datetime import datetime
from src.utils.logger_config import get_logger, PerformanceLogger


class APIClient:
    """Client for interacting with the alerts API"""
    
    def __init__(self, base_url: str, alerts_endpoint: str, secondary_video_endpoint: str,
                 device_id: str,
                 side_videos_endpoint: Optional[str] = None,
                 tasks_api_base_url: Optional[str] = None, tasks_endpoint: Optional[str] = None,
                 task_status_endpoint: Optional[str] = None, store_code: Optional[str] = None):
        """
        Initialize API client

        Args:
            base_url: Base URL of the API (e.g., http://49.13.89.74:8080)
            alerts_endpoint: Endpoint for fetching alerts (e.g., /api/alerts)
            secondary_video_endpoint: Endpoint template for updating secondary video (e.g., /api/alerts/{alert_id}/secondary-video)
            device_id: Device ID to include in X-DEVICE-ID header
            side_videos_endpoint: Endpoint for fetching side videos (e.g., /side-videos)
            tasks_api_base_url: Base URL for tasks API (e.g., https://u80w48ofg1.execute-api.eu-south-2.amazonaws.com)
            tasks_endpoint: Endpoint for fetching tasks (e.g., /api/tasks)
            task_status_endpoint: Endpoint template for task status (e.g., /api/status/{task_id})
            store_code: Store code to use in query parameters
        """
        self.base_url = base_url.rstrip('/')
        self.alerts_endpoint = alerts_endpoint
        self.secondary_video_endpoint = secondary_video_endpoint
        self.side_videos_endpoint = side_videos_endpoint or "/side-videos"
        self.device_id = device_id
        
        # Get API key from environment variable
        api_key = os.environ.get("STOREYES_API_KEY")
        if api_key:
            api_key = api_key.strip()
        self.api_key = api_key if api_key else None
        
        self.tasks_api_base_url = tasks_api_base_url.rstrip('/') if tasks_api_base_url else None
        self.tasks_endpoint = tasks_endpoint or "/api/tasks"
        self.task_status_endpoint = task_status_endpoint or "/api/status/{task_id}"
        self.store_code = store_code
        self.logger = get_logger(__name__)
        
        # Log API key status
        if self.api_key:
            self.logger.debug("API key found in environment variable")
        else:
            self.logger.debug("API key not found in environment variable (STOREYES_API_KEY), requests will not include X-API-KEY header")
    
    def _get_headers(self) -> Dict[str, str]:
        """
        Get default headers for API requests
        
        Returns:
            Dictionary with headers including X-DEVICE-ID and optionally X-API-KEY
        """
        headers = {
            "X-DEVICE-ID": self.device_id
        }
        
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        
        return headers
    
    def get_global_settings(self) -> Optional[Dict]:
        """
        Fetch global settings from the panel API
        
        Returns:
            Dictionary with global settings or None if fetch fails
        """
        url = "https://panel.storeyes.io/api/device-gw/settings"
        
        self.logger.debug(f"Fetching global settings from {url}")
        
        try:
            with PerformanceLogger(self.logger, "get_global_settings"):
                headers = self._get_headers()
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                settings = response.json()
            
            self.logger.info("Successfully fetched global settings from API")
            return settings
        except requests.RequestException as e:
            self.logger.warning(f"Failed to fetch global settings from API: {e}")
            return None
        except Exception as e:
            self.logger.warning(f"Unexpected error fetching global settings: {e}")
            return None
    
    def get_alerts(self, date: str) -> List[Dict]:
        """
        Fetch alerts for a specific date
        
        Args:
            date: Date in ISO format (e.g., 2025-12-10T00:00:00)
        
        Returns:
            List of alert dictionaries

        Raises:
            requests.RequestException: If the API request fails
        """
        url = f"{self.base_url}{self.alerts_endpoint}"
        params = {"date": date}

        self.logger.info(f"Fetching alerts from {url}", extra={"date": date})

        try:
            with PerformanceLogger(self.logger, "get_alerts", date=date):
                headers = self._get_headers()
                response = requests.get(url, params=params, headers=headers, timeout=30)
                response.raise_for_status()
                alerts = response.json()
            
            self.logger.info(f"Retrieved alerts", extra={"alert_count": len(alerts)})
            return alerts
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch alerts: {e}", exc_info=True)
            raise

    def get_side_videos(self, date: str) -> List[Dict]:
        """
        Fetch side videos for a specific date

        Args:
            date: Date in YYYY-MM-DD format

        Returns:
            List of side-video dictionaries (id, date, birthTime, part, name, videoUrl, ...)

        Raises:
            requests.RequestException: If the API request fails
        """
        url = f"{self.base_url}{self.side_videos_endpoint}"
        params = {"date": date}

        self.logger.info(f"Fetching side videos from {url}", extra={"date": date})

        try:
            with PerformanceLogger(self.logger, "get_side_videos", date=date):
                headers = self._get_headers()
                response = requests.get(url, params=params, headers=headers, timeout=30)
                response.raise_for_status()
                side_videos = response.json()

            self.logger.info(f"Retrieved side videos", extra={"side_video_count": len(side_videos)})
            return side_videos
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch side videos: {e}", exc_info=True)
            raise

    def update_secondary_video(self, alert_id: int, secondary_video_url: str, image_url: str) -> bool:
        """
        Update the secondary video URL and image URL for an alert
        
        Args:
            alert_id: ID of the alert to update
            secondary_video_url: S3 URL of the secondary video
            image_url: S3 URL of the image
            
        Returns:
            True if update was successful, False otherwise
            
        Raises:
            requests.RequestException: If the API request fails
        """
        url = f"{self.base_url}{self.secondary_video_endpoint.format(alert_id=alert_id)}"
        payload = {
            "secondaryVideoUrl": secondary_video_url,
            "imageUrl": image_url
        }
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        
        self.logger.info(
            f"Updating alert with secondary video URL",
            extra={"alert_id": alert_id, "video_url": secondary_video_url, "image_url": image_url}
        )
        
        try:
            with PerformanceLogger(self.logger, "update_secondary_video", alert_id=alert_id):
                response = requests.put(url, json=payload, headers=headers, timeout=30)
                response.raise_for_status()
            
            self.logger.info(f"Successfully updated alert", extra={"alert_id": alert_id})
            return True
        except requests.RequestException as e:
            error_extra = {"alert_id": alert_id}
            if hasattr(e, 'response') and e.response is not None:
                error_extra["response_status"] = e.response.status_code
                error_extra["response_body"] = e.response.text
            
            self.logger.error(f"Failed to update alert: {e}", extra=error_extra, exc_info=True)
            raise
    
    def get_tasks(self) -> Dict:
        """
        Fetch all tasks from the API
        
        Returns:
            Dictionary with "tasks" key containing list of task dictionaries
            
        Raises:
            requests.RequestException: If the API request fails
        """
        if not self.tasks_api_base_url:
            raise ValueError("Tasks API base URL not configured")
        
        url = f"{self.tasks_api_base_url}{self.tasks_endpoint}"
        
        # Build query parameters
        params = {}
        if self.store_code:
            params["store_code"] = self.store_code
        params["type"] = "outcome-comparator"
        
        self.logger.info(f"Fetching tasks from {url}", extra={"params": params})
        
        try:
            with PerformanceLogger(self.logger, "get_tasks"):
                headers = self._get_headers()
                response = requests.get(url, params=params, headers=headers, timeout=30)
                response.raise_for_status()
                tasks_data = response.json()
            
            task_count = len(tasks_data.get("tasks", []))
            self.logger.debug(f"Retrieved tasks", extra={"task_count": task_count})
            return tasks_data
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch tasks: {e}", exc_info=True)
            raise
    
    def get_task_status(self, task_id: str) -> Dict:
        """
        Get the status of a specific task
        
        Args:
            task_id: ID of the task to check
            
        Returns:
            Dictionary with task status information including "status" field
            
        Raises:
            requests.RequestException: If the API request fails
        """
        if not self.tasks_api_base_url:
            raise ValueError("Tasks API base URL not configured")
        
        url = f"{self.tasks_api_base_url}{self.task_status_endpoint.format(task_id=task_id)}"
        
        # Build query parameters
        params = {}
        if self.store_code:
            params["store_code"] = self.store_code
        
        self.logger.info(f"Fetching task status from {url}", extra={"task_id": task_id, "params": params})
        
        try:
            with PerformanceLogger(self.logger, "get_task_status", task_id=task_id):
                headers = self._get_headers()
                response = requests.get(url, params=params, headers=headers, timeout=30)
                response.raise_for_status()
                status_data = response.json()
            
            status = status_data.get("status", "unknown")
            self.logger.debug(f"Retrieved task status", extra={"task_id": task_id, "status": status})
            return status_data
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch task status: {e}", extra={"task_id": task_id}, exc_info=True)
            raise