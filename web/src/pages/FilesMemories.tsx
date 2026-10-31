import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import {
  Upload,
  Download,
  Trash2,
  RotateCcw,
  Paperclip,
  Eye,
  X,
  Plus,
  Check,
  Pencil,
  FileText,
} from "lucide-react";
import { api, ApiError, download } from "../api";
import { useAction, useRemote } from "../hooks";
import { listOf, fileSchema, memorySchema } from "../types";
import {
  PageTitle,
  Refresh,
  ErrorNotice,
  Notice,
  Empty,
  bytes,
  date,
  Field,
  JsonView,
} from "../components";

const filesSchema = listOf(fileSchema);
const memoriesSchema = listOf(memorySchema);
export function Files({
  active,
  attached,
  onAttach,
  onFileDeleted,
}: {
  active: boolean;
  attached: string[];
  onAttach: (ids: string[]) => void;
  onFileDeleted: (id: string) => void;
}) {
  const files = useRemote("/api/files", filesSchema, active);
  const action = useAction();
  const input = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState("available");
  const [selected, setSelected] = useState<string[]>(attached);
  const [deleting, setDeleting] = useState<string>();
  const [preview, setPreview] = useState<{
    name: string;
    text?: string;
    image?: string;
  }>();
  useEffect(
    () => () => {
      if (preview?.image) URL.revokeObjectURL(preview.image);
    },
    [preview],
  );
  async function upload(file: File) {
    if (file.size > 25 * 1024 ** 2) {
      action.setError(
        new ApiError("file_too_large", "单个上传文件最大 25 MiB。"),
      );
      return;
    }
    await action.run(async () => {
      const form = new FormData();
      form.append("file", file);
      await api("/api/files", fileSchema, { method: "POST", body: form });
      await files.reload(true);
    }, "文件已上传；选中并附加后，任务才能读取。");
    if (input.current) input.current.value = "";
  }
  function closePreview() {
    if (preview?.image) URL.revokeObjectURL(preview.image);
    setPreview(undefined);
  }
  async function showPreview(file: z.infer<typeof fileSchema>) {
    await action.run(async () => {
      const image = [
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
      ].includes(file.media_type);
      const text =
        ["text/plain", "text/markdown", "text/csv"].includes(file.media_type) ||
        /\.(txt|md|csv)$/i.test(file.name);
      if (!image && !text)
        throw new ApiError(
          "download_only",
          "此文件请下载查看；主动内容不会在工作台内执行。",
        );
      if (file.size > 2 * 1024 ** 2)
        throw new ApiError("preview_size", "大于 2 MiB 的文件请下载查看。");
      const result = await fetch(
        `/api/files/${encodeURIComponent(file.id)}/download`,
        { credentials: "same-origin", cache: "no-store", redirect: "error" },
      );
      if (!result.ok)
        throw new ApiError("preview_failed", "暂时无法读取文件。");
      closePreview();
      setPreview(
        image
          ? { name: file.name, image: URL.createObjectURL(await result.blob()) }
          : { name: file.name, text: await result.text() },
      );
    });
  }
  const visible =
    files.data?.items.filter((file) =>
      tab === "trash"
        ? ["deleted", "trashed"].includes(file.status) || !!file.deleted_at
        : !["deleted", "trashed"].includes(file.status) && !file.deleted_at,
    ) ?? [];
  return (
    <>
      <PageTitle
        eyebrow="04 / FILES"
        title="交付看得见的结果"
        action={
          <div className="actions">
            <Refresh busy={files.loading} onClick={() => void files.reload()} />
            <button
              className="button"
              disabled={action.busy}
              onClick={() => input.current?.click()}
            >
              <Upload size={16} />
              {action.busy ? "正在处理…" : "上传文件"}
            </button>
          </div>
        }
      >
        文件只向明确附加它的任务开放。正式产物通过校验后才会显示。
      </PageTitle>
      <input
        className="sr-only"
        ref={input}
        type="file"
        aria-label="上传文件"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void upload(file);
        }}
      />
      <ErrorNotice error={files.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      <div className="files-toolbar">
        <div className="segmented">
          <button
            className={tab === "available" ? "selected" : ""}
            onClick={() => setTab("available")}
          >
            文件与产物
          </button>
          <button
            className={tab === "trash" ? "selected" : ""}
            onClick={() => setTab("trash")}
          >
            回收站 · 7 天
          </button>
        </div>
        {selected.length > 0 && (
          <button
            className="button secondary"
            onClick={() => onAttach(selected)}
          >
            <Paperclip size={16} />
            附加 {selected.length} 个文件到聊天
          </button>
        )}
      </div>
      <section className="panel file-table">
        <div className="file-table-head">
          <span>名称与版本</span>
          <span>大小 / 状态</span>
          <span>创建时间</span>
          <span>操作</span>
        </div>
        {visible.map((file) => (
          <article className="file-row" key={file.id}>
            <div className="file-name">
              {tab !== "trash" && (
                <input
                  type="checkbox"
                  aria-label={`选择 ${file.name}`}
                  checked={selected.includes(file.id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, file.id]
                        : selected.filter((id) => id !== file.id),
                    )
                  }
                />
              )}
              <FileText size={20} />
              <div>
                <strong>{file.name}</strong>
                <small>
                  版本 {file.version}
                  {file.task_id ? ` · 任务 ${file.task_id}` : ""}
                </small>
              </div>
            </div>
            <div>
              {bytes(file.size)}
              <small>
                {(
                  {
                    published: "正式产物",
                    ready: "可用",
                    uploaded: "已上传",
                    generated: "正式产物",
                    partial: "部分结果",
                    processing: "处理中",
                    failed: "处理失败",
                    deleted: "已删除",
                    trashed: "已删除",
                  } as Record<string, string>
                )[file.status] ?? file.status}
              </small>
            </div>
            <time>
              {date(tab === "trash" ? file.deleted_at : file.created_at)}
            </time>
            <div className="actions">
              {tab === "trash" ? (
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(async () => {
                      await api(
                        `/api/files/${encodeURIComponent(file.id)}/restore`,
                        z.unknown(),
                        { method: "POST" },
                      );
                      await files.reload(true);
                    }, "文件已恢复；旧审批和任务不会自动恢复。")
                  }
                >
                  <RotateCcw size={15} />
                  恢复
                </button>
              ) : (
                <>
                  <button
                    className="icon-button"
                    aria-label={`预览 ${file.name}`}
                    onClick={() => void showPreview(file)}
                  >
                    <Eye size={17} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`下载 ${file.name}`}
                    onClick={() =>
                      void action.run(() =>
                        download(
                          `/api/files/${encodeURIComponent(file.id)}/download`,
                          file.name,
                        ),
                      )
                    }
                  >
                    <Download size={17} />
                  </button>
                  <button
                    className="icon-button danger-text"
                    aria-label={`删除 ${file.name}`}
                    onClick={() => setDeleting(file.id)}
                  >
                    <Trash2 size={17} />
                  </button>
                </>
              )}
            </div>
            {deleting === file.id && (
              <div className="confirm-box file-delete-confirm">
                <h3>删除“{file.name}”？</h3>
                <p>
                  立即停止后续读取、清除解析缓存，并进入七天回收站。相关任务可能暂停。已有可见聊天、独立产物和已确认记忆仍保留；需要时可在相应页面清理。
                </p>
                <div className="actions">
                  <button
                    className="button danger"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await api(
                          `/api/files/${encodeURIComponent(file.id)}`,
                          z.unknown(),
                          { method: "DELETE" },
                        );
                        setSelected(selected.filter((id) => id !== file.id));
                        onFileDeleted(file.id);
                        setDeleting(undefined);
                        await files.reload(true);
                      }, "文件已移入回收站。")
                    }
                  >
                    确认删除
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => setDeleting(undefined)}
                  >
                    返回
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
        {!visible.length && (
          <Empty title={tab === "trash" ? "回收站为空" : "还没有文件"}>
            {tab === "trash"
              ? "到期后按保留规则清理。"
              : "支持文本 PDF、DOCX、XLSX、CSV、TXT、Markdown 等；单文件最大 25 MiB。"}
          </Empty>
        )}
      </section>
      {preview && (
        <section className="preview-panel panel" aria-label="文件预览">
          <div className="panel-top">
            <h2>{preview.name}</h2>
            <button
              className="icon-button"
              aria-label="关闭预览"
              onClick={closePreview}
            >
              <X size={20} />
            </button>
          </div>
          {preview.image ? (
            <img
              className="file-preview-image"
              src={preview.image}
              alt={preview.name}
            />
          ) : (
            <pre className="text-preview">{preview.text}</pre>
          )}
        </section>
      )}
    </>
  );
}

export function Memories({ active }: { active: boolean }) {
  const memories = useRemote("/api/memories", memoriesSchema, active);
  const action = useAction();
  const [tab, setTab] = useState("confirmed");
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState<string>();
  const [editText, setEditText] = useState("");
  const [removing, setRemoving] = useState<string>();
  const visible =
    memories.data?.items.filter((item) =>
      tab === "candidates"
        ? ["candidate", "pending"].includes(item.status)
        : ["confirmed", "active"].includes(item.status),
    ) ?? [];
  return (
    <>
      <PageTitle
        eyebrow="05 / MEMORY"
        title="记住你明确选择的事"
        action={
          <Refresh
            onClick={() => void memories.reload()}
            busy={memories.loading}
          />
        }
      >
        每条记忆有来源，可编辑、可删除。自动提取只会生成待确认候选。
      </PageTitle>
      <ErrorNotice error={memories.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      <form
        className="memory-composer panel"
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            await api("/api/memories", memorySchema, {
              method: "POST",
              body: { content },
            });
            setContent("");
            await memories.reload(true);
          }, "记忆已保存。");
        }}
      >
        <Field label="明确记住">
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="例如：研究摘要优先使用中文，并保留原始来源。"
            required
            maxLength={10000}
            rows={3}
          />
        </Field>
        <button className="button" disabled={action.busy || !content.trim()}>
          <Plus size={16} />
          保存这条记忆
        </button>
      </form>
      <div className="segmented">
        <button
          className={tab === "confirmed" ? "selected" : ""}
          onClick={() => setTab("confirmed")}
        >
          已确认
        </button>
        <button
          className={tab === "candidates" ? "selected" : ""}
          onClick={() => setTab("candidates")}
        >
          待确认候选
        </button>
      </div>
      <div className="memory-list">
        {visible.map((item) => (
          <article className="memory-item panel" key={item.id}>
            <div className="panel-top">
              <span
                className={`status ${tab === "candidates" ? "status-waiting_approval" : "status-succeeded"}`}
              >
                {tab === "candidates" ? "等待确认" : "已确认"}
              </span>
              <time>{date(item.created_at)}</time>
            </div>
            {editing === item.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action.run(async () => {
                    await api(
                      `/api/memories/${encodeURIComponent(item.id)}`,
                      memorySchema,
                      {
                        method: "PATCH",
                        body: {
                          expected_revision: item.revision,
                          content: editText,
                        },
                      },
                    );
                    setEditing(undefined);
                    await memories.reload(true);
                  }, "记忆已更新。");
                }}
              >
                <Field label="编辑记忆">
                  <textarea
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    rows={4}
                    required
                    maxLength={10000}
                  />
                </Field>
                <div className="actions">
                  <button className="button" disabled={action.busy}>
                    保存修改
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => setEditing(undefined)}
                  >
                    返回
                  </button>
                </div>
              </form>
            ) : (
              <p className="memory-content">{item.content}</p>
            )}
            <details>
              <summary>查看来源与修订</summary>
              <p className="muted">修订 {item.revision}</p>
              <JsonView value={item.source} />
            </details>
            <div className="actions">
              {tab === "candidates" && (
                <button
                  className="button"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(async () => {
                      await api(
                        `/api/memories/${encodeURIComponent(item.id)}`,
                        memorySchema,
                        {
                          method: "PATCH",
                          body: {
                            expected_revision: item.revision,
                            status: "confirmed",
                          },
                        },
                      );
                      await memories.reload(true);
                    }, "已确认这条记忆。")
                  }
                >
                  <Check size={15} />
                  确认记住
                </button>
              )}
              <button
                className="text-button"
                onClick={() => {
                  setEditing(item.id);
                  setEditText(item.content);
                }}
              >
                <Pencil size={15} />
                编辑
              </button>
              <button
                className="text-button danger-text"
                onClick={() => setRemoving(item.id)}
              >
                <Trash2 size={15} />
                {tab === "candidates" ? "拒绝候选" : "删除"}
              </button>
            </div>
            {removing === item.id && (
              <div className="confirm-box">
                <p>
                  移除后不再参与检索，同一来源不会自动再次提议这一条。已有聊天与备份不会被同步抹除。
                </p>
                <div className="actions">
                  <button
                    className="button danger"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await api(
                          `/api/memories/${encodeURIComponent(item.id)}`,
                          z.unknown(),
                          {
                            method: "DELETE",
                            body: { expected_revision: item.revision },
                          },
                        );
                        setRemoving(undefined);
                        await memories.reload(true);
                      }, "已移除并记录来源抑制。")
                    }
                  >
                    确认移除
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => setRemoving(undefined)}
                  >
                    返回
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
        {!visible.length && (
          <Empty
            title={tab === "candidates" ? "没有待确认候选" : "还没有已确认记忆"}
          >
            只有你明确保存或确认的内容才进入长期记忆。
          </Empty>
        )}
      </div>
    </>
  );
}
