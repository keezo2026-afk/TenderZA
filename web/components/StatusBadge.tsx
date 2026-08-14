const STYLES: Record<string, string> = {
  OPEN: "bg-emerald-100 text-emerald-800",
  CLOSING_SOON: "bg-amber-100 text-amber-800",
  CLOSED: "bg-slate-200 text-slate-600",
  EXTENDED: "bg-blue-100 text-blue-800",
  CANCELLED: "bg-red-100 text-red-800",
  AWARDED: "bg-purple-100 text-purple-800",
  RE_ADVERTISED: "bg-cyan-100 text-cyan-800",
  UNKNOWN: "bg-slate-100 text-slate-500",
};

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${
        STYLES[status] ?? STYLES.UNKNOWN
      }`}
    >
      {status.replace("_", " ")}
    </span>
  );
}
