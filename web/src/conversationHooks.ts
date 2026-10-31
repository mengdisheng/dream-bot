import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "./api";
import { useRemote } from "./hooks";
import { conversationPageSchema, messagePageSchema, type Conversation } from "./types";
import type { z } from "zod";

const conversationsPath = "/api/conversations?limit=50";
export function useConversations() {
  const latest = useRemote(conversationsPath, conversationPageSchema, false, 5000);
  const [loaded, setLoaded] = useState<{ items: Conversation[]; next: string | null }>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<unknown>();
  useEffect(() => {
    if (!latest.data) return;
    const page = latest.data;
    setLoaded((old) => {
      const merged = new Map((old?.items ?? []).map((item) => [item.id, item]));
      const gap = !!old?.items.length && page.has_more && !!page.items.length && !page.items.some((item) => merged.has(item.id));
      page.items.forEach((item) => merged.set(item.id, item));
      return { items: [...merged.values()], next: old && !gap ? old.next : page.has_more ? page.next_cursor : null };
    });
  }, [latest.data]);
  const byId = new Map((loaded?.items ?? []).map((item) => [item.id, item]));
  latest.data?.items.forEach((item) => byId.set(item.id, item));
  const items = [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
  const cursor = loaded ? loaded.next : latest.data?.next_cursor;
  async function loadMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const result = await api(`${conversationsPath}&cursor=${encodeURIComponent(cursor)}`, conversationPageSchema, { passive: true });
      setLoaded((previous) => {
        const merged = new Map((previous?.items ?? []).map((item) => [item.id, item]));
        result.items.forEach((item) => merged.set(item.id, item));
        return { items: [...merged.values()], next: result.has_more ? result.next_cursor : null };
      });
      setPageError(undefined);
    } catch (error) { setPageError(error); }
    finally { setLoadingMore(false); }
  }
  return { ...latest, items, loadMore, loadingMore, hasMore: !!cursor, error: pageError ?? latest.error };
}

type Message = z.infer<typeof messagePageSchema>["items"][number];
/** Keep loaded pages tied to their conversation, including late responses after a switch. */
export function useConversationMessages(conversationId: string | null | undefined, streaming: boolean) {
  const path = conversationId ? `/api/conversations/${encodeURIComponent(conversationId)}/messages?limit=50` : null;
  const latest = useRemote(path, messagePageSchema, false, streaming ? 0 : 5000);
  const currentId = useRef(conversationId);
  useLayoutEffect(() => { currentId.current = conversationId; }, [conversationId]);
  const [loaded, setLoaded] = useState<{ id: string; items: Message[]; before: string | null }>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<unknown>();
  const page = latest.dataPath === path ? latest.data : undefined;
  const previous = loaded?.id === conversationId ? loaded : undefined;
  useEffect(() => {
    if (!conversationId || !page) return;
    setLoaded((old) => {
      const same = old?.id === conversationId;
      const merged = new Map((same ? old.items : []).map((item) => [item.id, item]));
      const gap = same && old.items.length > 0 && page.has_more && page.items.length > 0 && !page.items.some((item) => merged.has(item.id));
      page.items.forEach((item) => merged.set(item.id, item));
      return { id: conversationId, items: [...merged.values()], before: same && !gap ? old.before : page.has_more ? page.next_before : null };
    });
  }, [conversationId, page]);
  const merged = new Map((previous?.items ?? []).map((item) => [item.id, item]));
  page?.items.forEach((item) => merged.set(item.id, item));
  const items = [...merged.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  const before = previous ? previous.before : page?.next_before;
  useEffect(() => { setPageError(undefined); setLoadingMore(false); }, [conversationId]);
  const reloadLatest = latest.reload;
  const reload = useCallback((passive = true) => reloadLatest(passive), [reloadLatest]);
  async function loadMore() {
    if (!path || !conversationId || !before || loadingMore) return;
    setLoadingMore(true);
    try {
      const result = await api(`${path}&before=${encodeURIComponent(before)}`, messagePageSchema, { passive: true });
      if (currentId.current !== conversationId) return;
      setLoaded((old) => ({ id: conversationId, items: [...result.items, ...(old?.id === conversationId ? old.items : [])], before: result.has_more ? result.next_before : null }));
      setPageError(undefined);
    } catch (error) { if (currentId.current === conversationId) setPageError(error); }
    finally { if (currentId.current === conversationId) setLoadingMore(false); }
  }
  return { ...latest, reload, items, hasMore: !!before, loadMore, loadingMore, error: pageError ?? latest.error };
}
