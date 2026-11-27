import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-xl flex-1 flex-col justify-center gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">VBall</h1>
      <p className="text-slate-600 dark:text-slate-400">
        Upload a match and get every rally found automatically, ready to review, trim and export as a
        highlight video.
      </p>
      <Link
        href="/videos"
        className="inline-flex h-12 w-fit items-center rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-700"
      >
        Go to my videos
      </Link>
    </main>
  );
}
