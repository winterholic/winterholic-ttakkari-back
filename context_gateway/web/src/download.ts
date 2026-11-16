import type { GatewayClient } from "./api/client";
import type { DownloadedFile, ExportFormat } from "./api/types";

const UNSAFE_NAME = /[\\/:*?"<>|\u0000-\u001f]/g;

export function textFile(filename: string, text: string, mime: string): DownloadedFile {
  return { blob: new Blob([text], { type: mime }), filename: filename.replace(UNSAFE_NAME, "_") };
}

/** blob: URL로 만든 앵커를 눌러 저장한다. 인증은 이미 fetch에서 끝났으므로 URL에는 자격이 없다. */
export function saveBlob(file: DownloadedFile): void {
  const url = URL.createObjectURL(file.blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // 일부 브라우저는 click 직후 URL을 풀면 저장이 끊긴다.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function downloadExport(client: GatewayClient, projectId: string, format: ExportFormat, snapshotId: string | null, save: (file: DownloadedFile) => void = saveBlob): Promise<DownloadedFile> {
  const file = await client.download(projectId, format, snapshotId);
  save(file);
  return file;
}
