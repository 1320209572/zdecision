import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import type { CaptureRequestView, ProgressEvent } from "../../api/types";


interface CaptureCursor {
  request_id: string;
  repository_id: string;
  last_sequence: number;
}

interface CandidateRefreshState {
  running: boolean;
  message: string | null;
  failed: boolean;
  refresh: () => Promise<void>;
}

const terminalSuccess = new Set(["succeeded", "succeeded_no_candidates"]);
const terminalFailure = new Set(["failed_terminal", "cancelled"]);

function storageKey(repositoryId: string): string {
  return `zdecision:capture:${repositoryId}`;
}

function readCursor(repositoryId: string): CaptureCursor | null {
  try {
    const raw = localStorage.getItem(storageKey(repositoryId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<CaptureCursor>;
    if (
      typeof value.request_id !== "string" ||
      value.repository_id !== repositoryId ||
      typeof value.last_sequence !== "number" ||
      value.last_sequence < 0
    ) {
      return null;
    }
    return value as CaptureCursor;
  } catch {
    return null;
  }
}

function writeCursor(cursor: CaptureCursor): void {
  localStorage.setItem(storageKey(cursor.repository_id), JSON.stringify(cursor));
}

export function useCandidateRefresh(
  repositoryId: string,
  onSucceeded: () => void,
): CandidateRefreshState {
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<number | null>(null);
  const generation = useRef(0);

  const reconnect = useCallback(
    async (cursor: CaptureCursor, expectedGeneration: number) => {
      if (generation.current !== expectedGeneration) return;
      setRunning(true);
      setFailed(false);
      setMessage("正在等待本地设备处理更新");
      try {
        const result = await api<{ events: ProgressEvent[] }>(
          `/api/v1/capture-requests/${cursor.request_id}/events?after_sequence=${cursor.last_sequence}`,
        );
        if (generation.current !== expectedGeneration) return;
        const latest = result.events.at(-1);
        if (latest) {
          cursor = { ...cursor, last_sequence: latest.sequence };
          writeCursor(cursor);
          if (terminalSuccess.has(latest.state)) {
            localStorage.removeItem(storageKey(cursor.repository_id));
            setRunning(false);
            setMessage(
              latest.state === "succeeded_no_candidates"
                ? "更新完成，未发现新候选决策"
                : "候选决策更新完成",
            );
            onSucceeded();
            return;
          }
          if (terminalFailure.has(latest.state)) {
            localStorage.removeItem(storageKey(cursor.repository_id));
            setRunning(false);
            setFailed(true);
            setMessage("候选决策更新失败");
            return;
          }
          setMessage(`更新处理中 · ${latest.code}`);
        }
        timer.current = window.setTimeout(
          () => void reconnect(cursor, expectedGeneration),
          1000,
        );
      } catch {
        if (generation.current !== expectedGeneration) return;
        setRunning(false);
        setFailed(true);
        setMessage("无法恢复候选决策更新进度");
      }
    },
    [onSucceeded],
  );

  useEffect(() => {
    const currentGeneration = ++generation.current;
    setRunning(false);
    setMessage(null);
    setFailed(false);
    if (!repositoryId) return;
    const cursor = readCursor(repositoryId);
    if (cursor) void reconnect(cursor, currentGeneration);
    return () => {
      if (generation.current === currentGeneration) generation.current += 1;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [reconnect, repositoryId]);

  const refresh = useCallback(async () => {
    if (!repositoryId || running) return;
    const currentGeneration = generation.current;
    setRunning(true);
    setFailed(false);
    setMessage("正在创建候选决策更新");
    try {
      const request = await api<CaptureRequestView>("/api/v1/capture-requests", {
        method: "POST",
        body: JSON.stringify({
          repository_id: repositoryId,
          template_id: "business",
          capture_scope: "all_valid_sessions",
          client_action_id: `web_action_${crypto.randomUUID()}`,
        }),
      });
      if (generation.current !== currentGeneration) return;
      const cursor: CaptureCursor = {
        request_id: request.request_id,
        repository_id: repositoryId,
        last_sequence: request.last_sequence,
      };
      writeCursor(cursor);
      await reconnect(cursor, currentGeneration);
    } catch {
      if (generation.current !== currentGeneration) return;
      setRunning(false);
      setFailed(true);
      setMessage("候选决策更新失败");
    }
  }, [reconnect, repositoryId, running]);

  return { running, message, failed, refresh };
}
