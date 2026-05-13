import { permanentRedirect } from "next/navigation";

export const dynamic = "force-dynamic";

/**
 * `/status` was retired in the sidebar cutover (PR B + C of the nav
 * refactor). Per-bot runtime info now lives in each `BotCard` on
 * `/settings/bots`; the tenant-wide LiveLog moved to a collapsible
 * panel on the same page. Operators commonly bookmarked `/status` so
 * we 308 here rather than 404.
 */
export default function StatusPage(): never {
  permanentRedirect("/settings/bots");
}
