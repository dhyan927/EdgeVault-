import { useCallback, useEffect, useRef, useState } from "react";

// Change this if the hub runs on another machine/port.
export const HUB = "localhost:9000";

/**
 * Connects to the hub WebSocket and keeps `state` and `log` up to date.
 * Auto-reconnects if the hub restarts.
 *
 * Messages from hub:  {kind: "state" | "event" | "history", data: ...}
 *
 * onEvent (optional): called for every NEW live event (not for the old
 * history sent when the page loads). The dashboard uses it for pop-up alerts.
 */
export function useHub(onEvent) {
  const [state, setState] = useState(null);
  const [log, setLog] = useState([]);
  const [connected, setConnected] = useState(false);
  const [videoSources, setVideoSources] = useState([]);
  const [videoSourcesError, setVideoSourcesError] = useState("");
  const wsRef = useRef(null);
  const onEventRef = useRef(onEvent);

  // always call the latest callback without reconnecting
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    let stop = false;
    let timer;

    function connect() {
      const ws = new WebSocket(`ws://${HUB}/ws`);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!stop) timer = setTimeout(connect, 1500);
      };
      ws.onmessage = (m) => {
        const { kind, data } = JSON.parse(m.data);
        if (kind === "state") setState({ ...data });
        else if (kind === "history") setLog(data.slice(-100));
        else if (kind === "event") {
          // stamp the arrival time so the timeline doesn't depend on the hub's clock or timezone
          const ev = { ...data, _t: Date.now() };
          setLog((l) => [...l.slice(-99), ev]);
          onEventRef.current?.(ev);
        }
      };
    }
    connect();
    return () => {
      stop = true;
      clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  const request = useCallback(async (path, options) => {
    const response = await fetch(`http://${HUB}${path}`, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Hub request failed (${response.status})`);
    }
    return response.json();
  }, []);

  const refreshVideoSources = useCallback(async () => {
    try {
      const sources = await request("/video_sources");
      setVideoSources(sources);
      setVideoSourcesError("");
      return sources;
    } catch (error) {
      setVideoSourcesError(error.message);
      throw error;
    }
  }, [request]);

  useEffect(() => {
    refreshVideoSources().catch((error) => {
      console.error("Could not load video sources:", error);
    });
  }, [refreshVideoSources]);

  const trigger = useCallback((path) => request(`/trigger/${path}`, { method: "POST" }), [request]);
  const setVideoSource = useCallback((cam, video) => request("/camera_video", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cam, video }),
  }), [request]);
  const uploadVideo = useCallback(async (file) => {
    const result = await request(`/video_sources?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    await refreshVideoSources();
    return result;
  }, [refreshVideoSources, request]);
  const reportVideoFrames = useCallback(async (cam, frames) => {
    await request("/camera_analysis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cam, frames }),
    });
  }, [request]);

  return {
    state, log, connected, trigger, videoSources, videoSourcesError,
    setVideoSource, uploadVideo, reportVideoFrames,
  };
}
