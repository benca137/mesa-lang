#include "mesa_gc_runtime.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

Mesa_GC_Obj* _mesa_gc_head = NULL;
size_t _mesa_gc_bytes = 0;
Mesa_GC_Frame* _mesa_gc_stack = NULL;
const int _mesa_gc_desc_none[] = {-1};
Mesa_AllocContext _mesa_alloc_stack[256] = {{0}};
int _mesa_alloc_stack_len = 0;

static Mesa_GC_Obj* mesa_gc_find_obj(void* ptr) {
    Mesa_GC_Obj* obj;

    if (!ptr) {
        return NULL;
    }
    for (obj = _mesa_gc_head; obj; obj = obj->next) {
        if ((void*)(obj + 1) == ptr) {
            return obj;
        }
    }
    return NULL;
}

static void mesa_gc_mark(Mesa_GC_Obj* obj);

static void mesa_gc_mark_ptr(void* ptr) {
    Mesa_GC_Obj* obj = mesa_gc_find_obj(ptr);

    if (obj) {
        mesa_gc_mark(obj);
    }
}

static void mesa_gc_mark(Mesa_GC_Obj* obj) {
    char* base;
    int i;

    if (!obj || obj->mark) {
        return;
    }
    obj->mark = 1;
    if (!obj->desc) {
        return;
    }
    base = (char*)(obj + 1);
    for (i = 0; obj->desc[i] >= 0; i++) {
        void* child = *(void**)(base + obj->desc[i]);
        mesa_gc_mark_ptr(child);
    }
}

static void mesa_gc_collect(void) {
    Mesa_GC_Frame* frame = _mesa_gc_stack;
    Mesa_GC_Obj** cur = &_mesa_gc_head;

    while (frame) {
        int i;

        for (i = 0; i < frame->count; i++) {
            void* ptr = *frame->roots[i];
            mesa_gc_mark_ptr(ptr);
        }
        frame = frame->prev;
    }

    while (*cur) {
        if (!(*cur)->mark) {
            Mesa_GC_Obj* dead = *cur;
            *cur = dead->next;
            _mesa_gc_bytes -= dead->size;
            free(dead->base);
            continue;
        }
        (*cur)->mark = 0;
        cur = &(*cur)->next;
    }
}

static void* mesa_runtime_alloc_aligned(size_t size, size_t align) {
    size_t actual_size = size > 0 ? size : 1;
    size_t actual_align = align > sizeof(void*) ? align : sizeof(void*);
    void* ptr = NULL;

    if (posix_memalign(&ptr, actual_align, actual_size) != 0) {
        fprintf(stderr, "posix_memalign failed\n");
        abort();
    }
    return ptr;
}

void mesa_gc_push(Mesa_GC_Frame* f, void*** roots, int count) {
    f->roots = roots;
    f->count = count;
    f->prev = _mesa_gc_stack;
    _mesa_gc_stack = f;
}

void mesa_gc_pop(void) {
    if (_mesa_gc_stack) {
        _mesa_gc_stack = _mesa_gc_stack->prev;
    }
}

void* mesa_gc_alloc(size_t size, size_t align, const int* desc) {
    size_t actual_align = align > sizeof(void*) ? align : sizeof(void*);
    size_t total = sizeof(Mesa_GC_Obj) + size + actual_align - 1;
    char* raw;
    uintptr_t payload_addr;
    Mesa_GC_Obj* obj;

    if (_mesa_gc_bytes + size > MESA_GC_THRESHOLD) {
        mesa_gc_collect();
    }

    raw = (char*)malloc(total);
    if (!raw) {
        fprintf(stderr, "GC: out of memory\n");
        abort();
    }

    payload_addr = ((uintptr_t)(raw + sizeof(Mesa_GC_Obj) + actual_align - 1))
        & ~((uintptr_t)actual_align - 1);
    obj = (Mesa_GC_Obj*)(payload_addr - sizeof(Mesa_GC_Obj));
    obj->next = _mesa_gc_head;
    obj->base = raw;
    obj->size = size;
    obj->mark = 0;
    obj->desc = desc;
    _mesa_gc_head = obj;
    _mesa_gc_bytes += size;
    memset((void*)payload_addr, 0, size);
    return (void*)payload_addr;
}

void mesa_allocctx_push(void* self, void* (*alloc)(void* self, size_t size, size_t align)) {
    if (_mesa_alloc_stack_len >= 256) {
        fprintf(stderr, "allocator context stack overflow\n");
        abort();
    }
    _mesa_alloc_stack[_mesa_alloc_stack_len].self = self;
    _mesa_alloc_stack[_mesa_alloc_stack_len].alloc = alloc;
    _mesa_alloc_stack_len++;
}

void mesa_allocctx_pop(void) {
    if (_mesa_alloc_stack_len > 0) {
        _mesa_alloc_stack_len--;
    }
}

Mesa_AllocContext* mesa_allocctx_escape_target(void) {
    if (_mesa_alloc_stack_len >= 2) {
        return &_mesa_alloc_stack[_mesa_alloc_stack_len - 2];
    }
    if (_mesa_alloc_stack_len == 1) {
        return &_mesa_alloc_stack[0];
    }
    return NULL;
}

void* mesa_allocctx_alloc(Mesa_AllocContext* c, size_t size, size_t align) {
    if (!c || !c->alloc) {
        return mesa_runtime_alloc_aligned(size, align);
    }
    return c->alloc(c->self, size, align);
}
