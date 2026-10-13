export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden className={className}>
      <rect width="32" height="32" rx="8" fill="hsl(var(--brand-600))" />
      <path d="M9 22V10h7.2c2.9 0 4.8 1.7 4.8 4.2 0 1.9-1.1 3.3-2.8 3.9L22 22h-3.3l-3-3.6H12V22H9Zm3-6.1h4c1.2 0 2-.7 2-1.7s-.8-1.7-2-1.7h-4v3.4Z" fill="#fff" />
      <circle cx="24" cy="9" r="2.4" fill="hsl(var(--sidebar-active))" />
    </svg>
  );
}
