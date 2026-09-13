add_pk_library(
  tempos
  tempo_factory.cc
  base_tempo.cc
  msg_tempo.cc
  multi_tempo.cc
  file_tempo.cc
  final_tempo.cc
  inside_tempo.cc
  impt_tempo.cc
  time_tempo.cc
  exec_tempo.cc
  or_tempo.cc
)

target_link_libraries(tempos PRIVATE mktdata context pkcore)
