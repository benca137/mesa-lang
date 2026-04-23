#ifndef MESA_GC_RUNTIME_H
#define MESA_GC_RUNTIME_H

#include <stddef.h>

#ifndef MESA_GC_THRESHOLD
#define MESA_GC_THRESHOLD (1024 * 1024)
#endif

typedef struct Mesa_GC_Obj {
    struct Mesa_GC_Obj* next;
    void* base;
    size_t size;
    int mark;
    const int* desc;
} Mesa_GC_Obj;

typedef struct Mesa_GC_Frame {
    struct Mesa_GC_Frame* prev;
    void*** roots;
    int count;
} Mesa_GC_Frame;

extern Mesa_GC_Obj* _mesa_gc_head;
extern size_t _mesa_gc_bytes;
extern Mesa_GC_Frame* _mesa_gc_stack;
extern const int _mesa_gc_desc_none[];

void mesa_gc_push(Mesa_GC_Frame* f, void*** roots, int count);
void mesa_gc_pop(void);
void* mesa_gc_alloc(size_t size, size_t align, const int* desc);

typedef struct {
    void* self;
    void* (*alloc)(void* self, size_t size, size_t align);
} Mesa_AllocContext;

extern Mesa_AllocContext _mesa_alloc_stack[256];
extern int _mesa_alloc_stack_len;

void mesa_allocctx_push(void* self, void* (*alloc)(void* self, size_t size, size_t align));
void mesa_allocctx_pop(void);
Mesa_AllocContext* mesa_allocctx_escape_target(void);
void* mesa_allocctx_alloc(Mesa_AllocContext* c, size_t size, size_t align);

#endif /* MESA_GC_RUNTIME_H */
