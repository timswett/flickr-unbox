#!/usr/bin/perl
# Finds the photo_<id>.json sidecar for each photo/video file in PHOTOS_DIR,
# searching across one or more JSON_SEARCH_DIRS (the extracted info-zip parts).
# Copies matches into OUTPUT_JSON_DIR. Uses the same ID-matching rules as
# build_rename_plan.pl (video_<id> / <slug>_<id>_o / <slug>_<secret>_<id>_o),
# so this test fixture is derived the same way the real pipeline resolves IDs.
use strict;
use warnings;
use File::Copy;
use File::Find;

die "Usage: $0 <photos_dir> <output_json_dir> <report_tsv> <json_search_dir1> [json_search_dir2 ...]\n"
  unless @ARGV >= 4;

my ($photos_dir, $out_dir, $report, @search_dirs) = @ARGV;

mkdir $out_dir unless -d $out_dir;

# Index every photo_<id>.json found across all search dirs -> full path
my %json_index;
for my $dir (@search_dirs) {
  find(sub {
    return unless -f $_;
    if (/^photo_(\d+)\.json$/i) {
      $json_index{$1} = $File::Find::name;
    }
  }, $dir);
}

print "Indexed ", scalar(keys %json_index), " JSON sidecars across ", scalar(@search_dirs), " search dir(s)\n";

opendir(my $dh, $photos_dir) or die "Can't open $photos_dir: $!";
my @files = readdir($dh);
closedir($dh);

open(my $out, ">", $report) or die "Can't write $report: $!";
print $out "status\tphoto_file\tmatched_id\tjson_source\n";

my ($matched, $unresolved) = (0, 0);

for my $f (sort @files) {
  next if $f eq "." || $f eq "..";
  next if $f =~ /^\._/;      # AppleDouble junk
  next if $f eq ".DS_Store";
  next if -d "$photos_dir/$f";

  my $id;
  if ($f =~ /^video_(\d+)\.(\w+)$/i) {
    $id = $1;
  } elsif ($f =~ /_o\.(\w+)$/i) {
    (my $rem = $f) =~ s/_o\.\w+$//i;
    my @parts = split /_/, $rem;
    my @candidates;
    push @candidates, $parts[-1] if @parts >= 1 && $parts[-1] =~ /^\d+$/;
    push @candidates, $parts[-2] if @parts >= 2 && $parts[-2] =~ /^\d+$/;
    my @hits = grep { $json_index{$_} } @candidates;
    $id = $hits[0] if @hits;
  }

  if (!defined $id || !$json_index{$id}) {
    print $out "UNRESOLVED\t$f\t\t\n";
    $unresolved++;
    next;
  }

  my $src = $json_index{$id};
  my $dest = "$out_dir/photo_$id.json";
  if (!copy($src, $dest)) {
    print $out "COPY_FAILED\t$f\t$id\t$src\n";
    next;
  }
  print $out "OK\t$f\t$id\t$src\n";
  $matched++;
}

close($out);
print "Matched: $matched, Unresolved: $unresolved\n";
print "Matched JSON copied to: $out_dir\n";
print "Report: $report\n";
