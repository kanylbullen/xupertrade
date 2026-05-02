const TZ = "Europe/Stockholm";

export function formatDateTime(date: Date | string): string {
  return new Date(date).toLocaleString("sv-SE", {
    timeZone: TZ,
    hour12: false,
  });
}

export function formatDate(date: Date | string): string {
  return new Date(date).toLocaleDateString("sv-SE", { timeZone: TZ });
}

export function formatTime(date: Date | string): string {
  return new Date(date).toLocaleTimeString("sv-SE", {
    timeZone: TZ,
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}
