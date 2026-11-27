import {randomUUID} from 'node:crypto';

// Static embedded view only: opaque origin, no scripts, network or preload bridge.
// Preserve document bytes and visual styles; the user may open the original in
// their browser explicitly. These restrictions do not replace Electron updates.
export const artifactCsp = "default-src 'none'; script-src 'none'; script-src-attr 'none'; script-src-elem 'none'; worker-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; sandbox";
export class PreviewDocuments {
  private documents = new Map<string, string>();
  add(html: string): string {
    if (typeof html !== 'string' || Buffer.byteLength(html, 'utf8') > 1_000_000) throw new Error('HTML preview exceeds size limit');
    const url = `rocky-artifact://preview/${randomUUID()}`;
    this.documents.set(url, html);
    if (this.documents.size > 8) this.documents.delete(this.documents.keys().next().value!);
    return url;
  }
  get(url: string) { return this.documents.get(url); }
  remove(url: string) { this.documents.delete(url); }
}
