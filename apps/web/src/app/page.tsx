import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-xl flex-1 flex-col justify-center gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">VBall</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        Upload volleyball match videos and keep just the rallies. Uploads work; automatic rally
        detection is coming next.
      </p>
      <Link
        href="/videos"
        className="inline-flex h-12 w-fit items-center rounded-lg bg-zinc-900 px-5 font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
      >
        Go to my videos
      </Link>
    </main>
  );
}
